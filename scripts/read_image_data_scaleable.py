import rasterio
import numpy as np
import pandas as pd
import os
import shelve
import datetime
import bisect
from multiprocessing import Pool


def create_folders(shelve_dir):
    if not os.path.exists(shelve_dir):
        os.mkdir(shelve_dir)
    if not os.path.exists(shelve_dir + 'old/'):
        os.mkdir(shelve_dir + 'old/')
    if not os.path.exists(shelve_dir + 'train/'):
        os.mkdir(shelve_dir + 'train/')
    if not os.path.exists(shelve_dir + 'old/maps'):
        os.mkdir(shelve_dir + 'old/maps')
    if not os.path.exists(shelve_dir + 'old/maps_interpolated'):
        os.mkdir(shelve_dir + 'old/maps_interpolated')
    if not os.path.exists(shelve_dir + 'new/'):
        os.mkdir(shelve_dir + 'new/')
    if not os.path.exists(shelve_dir + 'new/maps'):
        os.mkdir(shelve_dir + 'new/maps')
    if not os.path.exists(shelve_dir + 'new/maps_interpolated'):
        os.mkdir(shelve_dir + 'new/maps_interpolated')


# define multiprocessing method for reading / combining raw images and masks
class ImageReader:
    def __init__(self, time_start, image_dir, mask_dir, shelve_dir):
        self.time_start = time_start
        self.mask_dir = mask_dir
        self.image_dir = image_dir
        self.shelve_dir = shelve_dir

    def __call__(self, fn):
        arr_img = rasterio.open(self.image_dir + fn).read()
        arr_msk = rasterio.open(self.mask_dir + fn).read()
        ts = self.time_start[self.time_start['system:index'] == fn.split('.')[0]]['system:time_start'].iloc[0]
        combo = np.concatenate((arr_img, arr_msk), axis=0)
        return str(ts), combo


def read_image_data(image_dir='2014/images/',
                    mask_dir='2014/masks/',
                    table_dir='2014/tables/LC8_SR.csv', 
                    shelve_dir=None,
                    processes=1):
    """
    reads data from raw images and store combined band and mask data in a shelve indexed by the image timestamp
    :param image_dir: directory of the images (GeoTiff) containing band info
    :param mask_dir: directory of the masks GeoTiff
    :param table_dir: path to the table with image metadata
    :param shelve_dir: directory for storing the shelf file containing processed data
    :param processes: number of processes to use
    :return: a Shelf View of the processed data, or a dict if shelve_dir is not specified
    """
    # combined = {}
    if shelve_dir is None:
        combined = {}  # write to file instead
    else:
        try:
            os.remove(shelve_dir + 'combined.*')
        except FileNotFoundError:
            pass
        combined = shelve.open(shelve_dir + 'combined')

    table = pd.read_csv(table_dir)
    time_start = table[['system:index', 'system:time_start']]

    p = Pool(processes)
    for ts, combo in p.imap(ImageReader(time_start, image_dir, mask_dir, shelve_dir), os.listdir(image_dir)):
        # combined[ts] = img
        fp = np.memmap(shelve_dir + 'maps/' + str(ts), dtype='int16', mode='w+', shape=combo.shape)
        fp[:] = combo[:]
        combined[ts] = combo.shape
        del fp, combo
    p.close()

    # for fn in os.listdir(image_dir):
    #     arr_img = rasterio.open(image_dir + fn).read()
    #     arr_msk = rasterio.open(mask_dir + fn).read()
    #     ts = time_start[time_start['system:index'] == fn.split('.')[0]]['system:time_start'].iloc[0]
    #     combined[str(ts)] = np.concatenate((arr_img, arr_msk), axis=0)

    return combined


def get_boolean_mask(image, level=1):
    """
    Generate a 1/0 cloud mask for a given image
    :param image: input image
    :param level: minimal confidence level
    :return: a mask matrix
    """
    # cfmask = image[3, :, :]
    # cfmask_conf = image[4, :, :]
    valid = (image[0, :, :] > 0)
    valid = valid & ((image[3, :, :] == 0) | (image[3, :, :] == 1))
    return valid & (image[4, :, :] <= level)


def zigzag_integer_pairs(max_x, max_y):
    """
    Generator
    Generate pairs of integers like (0,0), (0,1), (1,0), (0,2), (1,1), (2,0), ...
    Used for selecting images used for interpolation operations
    :param max_x: maximum number for the first element
    :param max_y: maximum number for the second element
    """
    total = 0
    x = 0
    while total <= max_x + max_y:
        if total - x <= max_y:
            yield (x, total - x)
        if x <= min(max_x - 1, total - 1):
            x += 1
        else:
            total += 1
            x = 0


def slice_indices(x, y, sx, sy):
    x_stops = range(0, x, sx)
    y_stops = range(0, y, sy)
    x_list = [(x_stops[i], x_stops[i+1]) for i in range(len(x_stops) - 1)] + [(x_stops[-1], x)]
    y_list = [(y_stops[i], y_stops[i+1]) for i in range(len(y_stops) - 1)] + [(y_stops[-1], y)]
    for x_range in x_list:
        for y_range in y_list:
            yield (x_range, y_range)


def slice_interpolation(image_before, image_after, xy, unfilled, alpha):
    x_range, y_range = xy
    slice_before = image_before[:, x_range[0]:x_range[1], y_range[0]:y_range[1]]
    slice_after = image_after[:, x_range[0]:x_range[1], y_range[0]:y_range[1]]
    mask_before = get_boolean_mask(slice_before)
    mask_after = get_boolean_mask(slice_after)
    common_unmasked = mask_before & mask_after
    del mask_before, mask_after
    valid = common_unmasked & unfilled[x_range[0]:x_range[1], y_range[0]:y_range[1]]
    del common_unmasked
    fitted = np.zeros((3, x_range[1] - x_range[0], y_range[1] - y_range[0]))
    fitted[:, valid] = slice_before[:3, valid] * alpha + slice_after[:3, valid] * (1 - alpha)
    return x_range, y_range, valid, fitted


class SliceInterpolator(object):
    def __init__(self, image_before, image_after, unfilled, alpha):
        self.image_before = image_before
        self.image_after = image_after
        self.unfilled = unfilled
        self.alpha = alpha

    def __call__(self, xy):
        return slice_interpolation(self.image_before, self.image_after, xy, self.unfilled, self.alpha)


def slice_filling(image_nearest, xy, unfilled):
    x_range, y_range = xy
    image_slice = image_nearest[:, x_range[0]:x_range[1], y_range[0]:y_range[1]]
    mask = get_boolean_mask(image_slice)
    valid = mask & unfilled[x_range[0]:x_range[1], y_range[0]:y_range[1]]
    return x_range, y_range, valid


class SliceFiller(object):
    def __init__(self, image_nearest, unfilled):
        self.image_nearest = image_nearest
        self.unfilled = unfilled

    def __call__(self, xy):
        return slice_filling(self.image_nearest, xy, self.unfilled)


def interpolate(timestamp, maps, max_days_apart=None, shelve_dir=None, processes=1, block_size=3000):
    # assuming dict keys are strings
    """
    Calculate the interpolated image at a given timestamp
    :param block_size: size of slices (for both x and y) into which the image is divided before interpolation
    :param processes: number of parallel processes
    :param shelve_dir: directory for shelf and memmap files
    :param timestamp: timestamp for interpolation (as int)
    :param maps: a dict or shelf view for image data, assuming timestamps are strings
    :param max_days_apart: maximum days allowed between two interpolated value and a known value to be used as input
    in interpolation before the algorithm reports a missing value
    :return: the interpolated image array
    """
    keys = list(maps.keys())
    times = [int(k) for k in keys]
    times.sort()
    pos = bisect.bisect(times, timestamp)
    # n_times = len(times)
    # dims = dataset[str(times[0])].shape
    dims = maps[next(iter(maps.keys()))]
    interpolated = np.ones((3, dims[1], dims[2]), dtype='int16') * (-9999)
    times_before = times[:pos]
    times_before.reverse()
    times_after = times[pos:]
    unfilled = np.ones(dims[1:], dtype=bool)
    for pair in zigzag_integer_pairs(len(times_before) - 1, len(times_after) - 1):
        before = times_before[pair[0]]
        after = times_after[pair[1]]
        delta = datetime.datetime.fromtimestamp(after/1000) - datetime.datetime.fromtimestamp(before/1000)
        if max_days_apart is None or delta.days < max_days_apart:
            alpha = 1.0 * (timestamp - before) / (after - before)
            image_before = np.memmap(shelve_dir + 'maps/' + str(before), dtype='int16', mode='r',
                                     shape=maps[str(before)])
            image_after = np.memmap(shelve_dir + 'maps/' + str(after), dtype='int16', mode='r', shape=maps[str(after)])
            # mask_before = get_boolean_mask(image_before)
            # mask_after = get_boolean_mask(image_after)
            # common_unmasked = mask_before & mask_after
            # del mask_before, mask_after
            # valid = common_unmasked & unfilled
            # del common_unmasked
            # #         fitted = dataset[before][:3, :, :] * alpha + dataset[after][:3, :, :] * (1 - alpha)
            # fitted = np.zeros((3, dims[1], dims[2]))
            # fitted[:, valid] = image_before[:3, valid] * alpha + image_after[:3, valid] * (1 - alpha)
            res_x, res_y = maps[str(before)][1:]
            # ranges = slice_indices(res_x, res_y, res_x, res_y)
            # p = Pool(processes)
            # for xr, yr in slice_indices(res_x, res_y, res_x, res_y):
            for xr, yr, valid, fitted in map(SliceInterpolator(image_before, image_after, unfilled, alpha),
                                             slice_indices(res_x, res_y, block_size, block_size)):
                # valid, fitted = slice_interpolation(image_before, image_after, (xr, yr), unfilled, alpha)
                unfilled[xr[0]:xr[1], yr[0]:yr[1]] = unfilled[xr[0]:xr[1], yr[0]:yr[1]] ^ valid
                interpolated[:, xr[0]:xr[1], yr[0]:yr[1]][:, valid] = fitted[:, valid]
            # p.close()
            del image_before, image_after
    times.sort(key=lambda t: abs(t - timestamp))
    for ts in times:
        delta = datetime.datetime.fromtimestamp(ts / 1000) - datetime.datetime.fromtimestamp(timestamp / 1000)
        if max_days_apart is None or abs(delta.days) < max_days_apart:
            image_nearest = np.memmap(shelve_dir + 'maps/' + str(ts), dtype='int16', mode='r', shape=maps[str(ts)])
            # mask = get_boolean_mask(image_nearest)
            # valid = mask & unfilled
            # unfilled = unfilled ^ valid
            # interpolated[:, valid] = image_nearest[:3, valid]
            # del image_nearest, mask, valid
            res_x, res_y = maps[str(ts)][1:]
            for xr, yr, valid in map(SliceFiller(image_nearest, unfilled),
                                     slice_indices(res_x, res_y, block_size, block_size)):
                # print("debug:")
                # print(unfilled[xr[0]:xr[1], yr[0]:yr[1]].shape)
                # print(interpolated[:, xr[0]:xr[1], yr[0]:yr[1]].shape)
                # print(image_nearest.shape)
                # print(valid.shape)
                unfilled[xr[0]:xr[1], yr[0]:yr[1]] = unfilled[xr[0]:xr[1], yr[0]:yr[1]] ^ valid
                interpolated[:, xr[0]:xr[1], yr[0]:yr[1]][:, valid] =\
                    image_nearest[:3, xr[0]:xr[1], yr[0]:yr[1]][:, valid]
            del image_nearest
    return interpolated


class Interpolater(object):

    def __init__(self, maps, max_days_apart=None, shelve_dir=None, processes=1, block_size=3000):
        self.maps = maps
        self.max_days_apart = max_days_apart
        self.shelve_dir = shelve_dir
        self.processes = processes
        self.block_size = block_size

    def __call__(self, ts):
        return str(ts), interpolate(ts, self.maps, self.max_days_apart, self.shelve_dir, self.processes,
                                    self.block_size)


def interpolate_images(timestamps, maps, max_days_apart=None, processes=1, shelve_dir=None, block_size=3000):
    if processes == 1:
        return {ts: interpolate(ts, maps, max_days_apart) for ts in timestamps}
    else:
        try:
            os.remove(shelve_dir + 'interpolated.*')
        except FileNotFoundError:
            pass
        imgs = shelve.open(shelve_dir + 'interpolated')
        p = Pool(processes)
        for ts, img in p.imap(Interpolater(maps, max_days_apart, shelve_dir, processes, block_size), timestamps):
            fp = np.memmap(shelve_dir + 'maps_interpolated/' + str(ts), dtype='int16', mode='w+', shape=img.shape)
            fp[:] = img[:]
            imgs[ts] = img.shape
            del fp, img
        p.close()
        return imgs


def open_img(img_id, img_dir, res=None):
    return np.memmap(img_dir + img_id, dtype='int16', mode='r', shape=res)


def generate_coordinate_columns(x, y):
    res = np.zeros((x * y, 2), dtype=int)
    res[:, 0] = np.array([i for i in range(x) for j in range(y)])
    res[:, 1] = np.array([i for i in range(y)] * x)
    return res


def extract_label_column(label_array):
    df = pd.DataFrame(label_array)
    df['x'] = df.index
    label_set = pd.melt(df, id_vars='x')
    return label_set.sort_values(by=['x', 'variable'])['value'].as_matrix()


def extract_partial_set(index_range, imgs, img_dir, labels=None):
    start, end = index_range
    items = list(imgs.items())
    items.sort()
    b, x, y = items[0][1]
    df = pd.DataFrame(generate_coordinate_columns(x, y)[start:end, :], columns=['x', 'y'])
    for i in range(len(items)):
        ts, res = items[i]
        img = open_img(ts, img_dir, (res[0], res[1] * res[2]))
        for band in range(3):
            df[ts + '_' + str(band)] = img[band, start: end]
    if labels is not None:
        df['label'] = extract_label_column(labels)[start: end]
    return df


def partial_set_iterator(step, imgs, img_dir, labels=None):
    ts0, res0 = (next(iter(imgs.items())))
    length = res0[1] * res0[2]
    if length < step:
        ranges = [(0, length)]
    else:
        stops = list(range(0, length, step))
        ranges = [(stops[i], stops[i+1]) for i in range(len(stops) - 1)] + [(stops[-1], length)]
    for index_range in ranges:
        yield index_range, extract_partial_set(index_range, imgs, img_dir, labels)


def store_set(step, imgs, img_dir, shelve_dir, name='trains', labels=None):
    try:
        os.remove(shelve_dir + name + '.*')
    except FileNotFoundError:
        pass
    trains = shelve.open(shelve_dir + name)
    n = 0
    for train in partial_set_iterator(step, imgs, img_dir, labels):
        trains[str(n)] = train
        n += 1
    return trains


def write_to_csv(step, imgs, img_dir, shelve_dir, name='trains', labels=None):
    try:
        os.remove(shelve_dir + name + '.csv')
    except FileNotFoundError:
        pass
    with open(shelve_dir + name + '.csv', 'a') as f:
        first = True
        for index_range, train in partial_set_iterator(step, imgs, img_dir, labels):
            if first:
                train.to_csv(f, header=True, index=False)
                first = False
            else:
                train.to_csv(f, header=False, index=False)


def old_data_preprocess_workflow(image_dir, mask_dir, table_dir, shelve_root_dir, labels, new_table_dir=None,
                                 max_days_apart=60, processes=1, step=250000, timestamps=None, to_csv=False,
                                 block_size=3000):
    """
    preprocess training data, interpolating it using the next year's available data timestamps
    :param block_size:
    :param to_csv: whether to write to a csv file. if False, output shelf file reference
    :param image_dir: directory of the image files
    :param mask_dir: directory of the mask files
    :param table_dir: path to the metadata table
    :param new_table_dir: path to the next year's metadata table (not used if timestamps are given)
    :param shelve_root_dir: root directory for shelf and memmap files to be created
    :param labels: class label map in array form
    :param max_days_apart: maximum days between interpolated value and an known value before reporting missing value
    :param processes: number of processes for multiprocessing
    :param step: batch size for training set generation
    :param timestamps: timestamps for interpolation (not necessary if new_table_dir is given)
    :return:
    """
    print("reading data...")
    maps = read_image_data(image_dir, mask_dir, table_dir, shelve_root_dir + 'old/', processes)
    print("reading new timestamps...")
    # res = ds[list(ds.keys())[0]].shape[1:]
    new_table = pd.read_csv(new_table_dir)
    new_times = list(new_table['system:time_start'])
    if timestamps is None:
        times_to_fit = []
        for t in new_times:
            dt = datetime.datetime.fromtimestamp(t / 1000)
            dt = dt.replace(year=dt.year - 1)
            times_to_fit += [int(dt.timestamp() * 1000)]
    else:
        times_to_fit = timestamps
    times_to_fit.sort()
    print("interpolating images...")
    imgs = interpolate_images(times_to_fit, maps, max_days_apart, processes, shelve_root_dir + 'old/', block_size)
    print("generating sets...")
    if not to_csv:
        trains = store_set(step, imgs, shelve_root_dir + 'old/maps_interpolated/', shelve_root_dir, 'trains', labels)
        return trains
    else:
        write_to_csv(step, imgs, shelve_root_dir + 'old/maps_interpolated/', shelve_root_dir, 'trains', labels)
        return True


def new_data_preprocess_workflow(image_dir, mask_dir, table_dir, shelve_root_dir,
                                 max_days_apart=60, processes=1, step=250000, to_csv=False, block_size=3000):
    """
    preprocess training data, interpolating it using the next year's available data timestamps
    :param to_csv: whether to write to a csv file. if False, output shelf file reference
    :param image_dir: directory of the image files
    :param mask_dir: directory of the mask files
    :param table_dir: path to the metadata table
    :param shelve_root_dir: root directory for shelf and memmap files to be created
    :param max_days_apart: maximum days between interpolated value and an known value before reporting missing value
    :param processes: number of processes for multiprocessing
    :param step: batch size for training set generation
    :return:
    """
    print("reading data...")
    maps = read_image_data(image_dir, mask_dir, table_dir, shelve_root_dir + 'new/', processes)
    print("reading new timestamps...")
    # res = ds[list(ds.keys())[0]].shape[1:]
    new_table = pd.read_csv(table_dir)
    times_to_fit = list(new_table['system:time_start'])
    times_to_fit.sort()
    print("interpolating images...")
    imgs = interpolate_images(times_to_fit, maps, max_days_apart, processes, shelve_root_dir + 'new/', block_size)
    print("generating sets...")
    if not to_csv:
        to_predict = store_set(step, imgs, shelve_root_dir + 'new/maps_interpolated/', shelve_root_dir, 'to_predict')
        return to_predict
    else:
        write_to_csv(step, imgs, shelve_root_dir + 'new/maps_interpolated/', shelve_root_dir, 'to_predict')
        return True
