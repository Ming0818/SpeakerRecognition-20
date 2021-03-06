from abc import ABCMeta, abstractmethod
import numpy as np
import librosa
from scipy import ndimage
import os
import random
from utils import bool_argument, eprint, listdir_files

# ======
# base class

class DataBase:
    __metaclass__ = ABCMeta

    def __init__(self, config):
        self.dataset = None
        self.num_epochs = None
        self.max_steps = None
        self.batch_size = None
        self.val_size = None
        self.packed = None
        self.processes = None
        self.threads = None
        self.prefetch = None
        self.buffer_size = None
        self.shuffle = None
        self.group_size = None
        self.num_labels = None
        # copy all the properties from config object
        self.config = config
        self.__dict__.update(config.__dict__)
        # initialize
        self.get_files()

    @staticmethod
    def add_arguments(argp, test=False):
        # base parameters
        bool_argument(argp, 'packed', False)
        bool_argument(argp, 'test', test)
        # pre-processing parameters
        argp.add_argument('--processes', type=int, default=2)
        argp.add_argument('--threads', type=int, default=1)
        argp.add_argument('--prefetch', type=int, default=64)
        argp.add_argument('--buffer-size', type=int, default=256)
        bool_argument(argp, 'shuffle', True)
        argp.add_argument('--group-size', type=int, default=4)
        # sample parameters
        argp.add_argument('--pp-rate', type=int, default=16000)
        argp.add_argument('--pp-duration', type=float,
            help='0: no slicing, -: fixed slicing, +: random slicing')
        argp.add_argument('--pp-smooth', type=float)
        argp.add_argument('--pp-noise', type=float)
        argp.add_argument('--pp-amplitude', type=int)

    @staticmethod
    def parse_arguments(args):
        def argdefault(name, value):
            if args.__getattribute__(name) is None:
                args.__setattr__(name, value)
        def argchoose(name, cond, tv, fv):
            argdefault(name, tv if cond else fv)
        argchoose('batch_size', args.test, 36, 72)
        argchoose('pp_duration', args.test, 2.0, 1.0)
        argchoose('pp_smooth', args.test, 0, 0)
        argchoose('pp_noise', args.test, 0, 0.7)
        argchoose('pp_amplitude', args.test, 0, 0)

    @staticmethod
    def group_shuffle(dataset, batch_size, shuffle, group_size):
        # random shuffle
        if shuffle:
            random.shuffle(dataset)
        # make sure the length is divisible by batch_size
        while len(dataset) % batch_size > 0:
            dataset.pop()
        upper = len(dataset)
        assert batch_size % group_size == 0
        batch_groups = batch_size // group_size
        # loop over batches
        for i in range(0, upper, batch_size):
            j = i + batch_groups
            ids = [d[0] for d in dataset[i : j]]
            counts = {ids[i]: group_size - 1 for i in range(batch_groups)}
            for k in range(j, upper):
                data = dataset[k]
                id_ = data[0]
                if id_ in ids and counts[id_] > 0:
                    counts[id_] -= 1
                    if counts[id_] <= 0:
                        ids.remove(id_)
                    dataset[k] = dataset[j]
                    dataset[j] = data
                    j += 1
                    if j >= i + batch_size:
                        break

    def get_files_packed(self):
        data_list = os.listdir(self.dataset)
        data_list = [os.path.join(self.dataset, i) for i in data_list]
        # val set
        if self.val_size is not None:
            self.val_steps = self.val_size // self.batch_size
            assert self.val_steps < len(data_list)
            self.val_size = self.val_steps * self.batch_size
            self.val_set = data_list[:self.val_steps]
            data_list = data_list[self.val_steps:]
            eprint('validation set: {}'.format(self.val_size))
        # main set
        self.epoch_steps = len(data_list)
        self.epoch_size = self.epoch_steps * self.batch_size
        if self.max_steps is None:
            self.max_steps = self.epoch_steps * self.num_epochs
        else:
            self.num_epochs = (self.max_steps + self.epoch_steps - 1) // self.epoch_steps
        self.main_set = data_list

    @abstractmethod
    def get_files_origin(self):
        pass

    def get_files(self):
        if self.packed: # packed dataset
            self.get_files_packed()
        else: # non-packed dataset
            data_list = self.get_files_origin()
            # val set
            if self.val_size is not None:
                assert self.val_size < len(data_list)
                self.val_steps = self.val_size // self.batch_size
                self.val_size = self.val_steps * self.batch_size
                self.val_set = data_list[:self.val_size]
                data_list = data_list[self.val_size:]
                eprint('validation set: {}'.format(self.val_size))
            # main set
            assert self.batch_size <= len(data_list)
            self.epoch_steps = len(data_list) // self.batch_size
            self.epoch_size = self.epoch_steps * self.batch_size
            if self.max_steps is None:
                self.max_steps = self.epoch_steps * self.num_epochs
            else:
                self.num_epochs = (self.max_steps + self.epoch_steps - 1) // self.epoch_steps
            self.main_set = data_list[:self.epoch_size]
        # print
        eprint('main set: {}\nepoch steps: {}\nnum epochs: {}\nmax steps: {}\n'
            .format(self.epoch_size, self.epoch_steps, self.num_epochs, self.max_steps))

    @staticmethod
    def process_sample(file, label, config):
        # parameters
        sample_rate = config.pp_rate if config.pp_rate > 0 else None
        slice_duration = np.abs(config.pp_duration)
        # slice
        duration = librosa.get_duration(filename=file)
        if config.pp_duration > 0 and duration > slice_duration:
            # randomly cropping
            offset = random.uniform(0, duration - slice_duration)
        else:
            offset = 0.0
        # read from file
        data, rate = librosa.load(file, sr=sample_rate, mono=True,
            offset=offset, duration=slice_duration)
        if len(data.shape) < 2:
            data = np.expand_dims(data, 0)
        audio_max = np.max(data)
        samples = data.shape[-1]
        slice_samples = int(slice_duration * rate + 0.5)
        # normalization
        if audio_max > 1e-6:
            norm_factor = 1 / audio_max
            data *= norm_factor
        # zero padding
        if samples < slice_samples:
            data = np.pad(data, ((0, 0), (0, slice_samples - samples)), 'constant')
        # random data manipulation
        data = DataPP.process(data, config)
        # return
        return data, label

    @classmethod
    def extract_batch(cls, batch_set, config):
        from concurrent.futures import ThreadPoolExecutor
        # initialize
        inputs = []
        labels = []
        # load all the data
        if config.threads == 1:
            for file, label in batch_set:
                _input, _label = cls.process_sample(file, label, config)
                inputs.append(_input)
                labels.append(_label)
        else:
            with ThreadPoolExecutor(config.threads) as executor:
                futures = []
                for file, label in batch_set:
                    futures.append(executor.submit(cls.process_sample, file, label, config))
                # final data
                while len(futures) > 0:
                    _input, _label = futures.pop(0).result()
                    inputs.append(_input)
                    labels.append(_label)
        # zero padding if not of the same length
        if config.pp_duration <= 0:
            max_samples = max([i.shape[-1] for i in inputs])
            inputs = [np.pad(i, (0, max_samples - i.shape[-1]), 'constant') for i in inputs]
        # stack data to form a batch (NCW)
        inputs = np.stack(inputs)
        labels = np.array(labels)
        # convert to NCHW format
        inputs = np.expand_dims(inputs, -2)
        # return
        return inputs, labels

    @classmethod
    def extract_batch_packed(cls, batch_set):
        npz = np.load(batch_set)
        inputs = npz['inputs']
        labels = npz['labels']
        return inputs, labels

    def _gen_batches_packed(self, dataset, epoch_steps, num_epochs=1, start=0):
        _dataset = dataset
        max_steps = epoch_steps * num_epochs
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(self.processes) as executor:
            futures = []
            # loop over epochs
            for epoch in range(start // epoch_steps, num_epochs):
                step_offset = epoch_steps * epoch
                step_start = max(0, start - step_offset)
                step_stop = min(epoch_steps, max_steps - step_offset)
                # loop over steps within an epoch
                for step in range(step_start, step_stop):
                    batch_set = _dataset[step]
                    futures.append(executor.submit(self.extract_batch_packed,
                        batch_set))
                    # yield the data beyond prefetch range
                    while len(futures) >= self.prefetch:
                        yield futures.pop(0).result()
            # yield the remaining data
            for future in futures:
                yield future.result()

    def _gen_batches_origin(self, dataset, epoch_steps, num_epochs=1, start=0,
        shuffle=False):
        _dataset = dataset
        max_steps = epoch_steps * num_epochs
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(self.processes) as executor:
            futures = []
            # loop over epochs
            for epoch in range(start // epoch_steps, num_epochs):
                step_offset = epoch_steps * epoch
                step_start = max(0, start - step_offset)
                step_stop = min(epoch_steps, max_steps - step_offset)
                # grouping random shuffle
                if epoch > 0:
                    _dataset = dataset.copy()
                    self.group_shuffle(_dataset, self.batch_size, shuffle, self.group_size)
                # loop over steps within an epoch
                for step in range(step_start, step_stop):
                    offset = step * self.batch_size
                    upper = min(len(_dataset), offset + self.batch_size)
                    batch_set = _dataset[offset : upper]
                    futures.append(executor.submit(self.extract_batch,
                        batch_set, self.config))
                    # yield the data beyond prefetch range
                    while len(futures) >= self.prefetch:
                        yield futures.pop(0).result()
            # yield the remaining data
            for future in futures:
                yield future.result()

    def _gen_batches(self, dataset, epoch_steps, num_epochs=1, start=0,
        shuffle=False):
        # packed dataset
        if self.packed:
            return self._gen_batches_packed(dataset, epoch_steps, num_epochs, start)
        else:
            return self._gen_batches_origin(dataset, epoch_steps, num_epochs, start, shuffle)

    def gen_main(self, start=0):
        return self._gen_batches(self.main_set, self.epoch_steps, self.num_epochs,
            start, self.shuffle)

    def gen_val(self, start=0):
        return self._gen_batches(self.val_set, self.val_steps, 1,
            start, False)

class DataPP:
    @classmethod
    def process(cls, data, config):
        # smoothing
        smooth_prob = config.pp_smooth
        smooth_std = 0.75
        if cls.active_prob(smooth_prob):
            smooth_scale = cls.truncate_normal(smooth_std)
            data = ndimage.gaussian_filter1d(data, smooth_scale, truncate=2.0)
        # add noise
        noise_prob = config.pp_noise
        noise_std = 0.01
        noise_smooth_prob = 0.8
        noise_smooth_std = 1.5
        while cls.active_prob(noise_prob):
            # Gaussian noise
            noise_scale = cls.truncate_normal(noise_std)
            noise = np.random.normal(0.0, noise_scale, data.shape)
            # noise smoothing
            if cls.active_prob(noise_smooth_prob):
                smooth_scale = cls.truncate_normal(noise_smooth_std)
                noise = ndimage.gaussian_filter1d(noise, smooth_scale, truncate=2.0)
            # add noise
            data += noise
        # random amplitude
        amplitude = config.pp_amplitude / 10
        if amplitude > 0:
            data *= 0.1 ** np.random.uniform(0, amplitude) # 0~-20 dB
        # return
        return data

    @staticmethod
    def active_prob(prob):
        return np.random.uniform(0, 1) < prob

    @staticmethod
    def truncate_normal(std, mean=0.0, max_rate=4.0):
        max_scale = std * max_rate
        scale = max_scale + 1.0
        while scale > max_scale:
            scale = np.abs(np.random.normal(0.0, std))
        scale += mean
        return scale

# ======
# derived classes

class DataVoxCeleb(DataBase):
    def get_files_origin(self):
        dataset_ids = os.listdir(self.dataset)
        dataset_ids = [os.path.join(self.dataset, i) for i in dataset_ids]
        dataset_ids = [i for i in dataset_ids if os.path.isdir(i)]
        num_labels = len(dataset_ids)
        self.num_labels = num_labels
        # data list
        data_list = []
        for i in range(num_labels):
            files = listdir_files(dataset_ids[i], filter_ext=['.wav', '.m4a', '.mp3'])
            for f in files:
                data_list.append((f, i))
        self.group_shuffle(data_list, self.batch_size, self.shuffle, self.group_size)
        # return
        return data_list

class DataSpeech(DataBase):
    @staticmethod
    def parse_arguments(args):
        def argdefault(name, value):
            if args.__getattribute__(name) is None:
                args.__setattr__(name, value)
        def argchoose(name, cond, tv, fv):
            argdefault(name, tv if cond else fv)
        argchoose('batch_size', args.test, 16, 32)
        argchoose('pp_duration', args.test, -4.0, -2.0)
        argchoose('pp_smooth', args.test, 0, 0)
        argchoose('pp_noise', args.test, 0, 0.7)
        argchoose('pp_amplitude', args.test, 0, 0)

    @staticmethod
    def ordered_ids(ids):
        # unique ids
        unique_ids = np.unique(ids)
        # map to ordered ids
        mapping = {d: i for i, d in enumerate(unique_ids)}
        ordered_ids = list(map(lambda x: mapping[x], ids))
        # return
        return ordered_ids

    def get_files_origin(self):
        import re
        # get all the audio files
        files = listdir_files(self.dataset, filter_ext=['.wav', '.m4a', '.mp3'])
        regex = re.compile(r'^(.*[/\\])?(.+?)[-_](.+?)(\..+?)$')
        matches = [re.findall(regex, f)[0][1:3] for f in files]
        person_ids, speech_ids = [self.ordered_ids(list(i)) for i in zip(*matches)]
        self.num_labels = max(speech_ids) + 1
        # data list
        data_list = [(files[i], speech_ids[i]) for i in range(len(files))]
        self.group_shuffle(data_list, self.batch_size, self.shuffle, self.group_size)
        # return
        return data_list
