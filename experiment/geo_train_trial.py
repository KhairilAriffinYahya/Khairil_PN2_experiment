import argparse
import os
import torch
import datetime
import logging
from pathlib import Path
import sys
import importlib
import shutil
import provider
import numpy as np
import time
from tqdm import tqdm
import laspy
import glob
from collections import Counter
from torch.utils.data import Dataset, DataLoader, random_split
import matplotlib.pyplot as plt
import time
import pickle
import open3d as o3d
import h5py

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BASE_DIR
sys.path.append(os.path.join(ROOT_DIR, 'models'))
o3sdevice = o3d.core.Device("CPU:0")
o3ddtype = o3d.core.float32

classes = ["wall", "window",  "door",  "molding", "other", "terrain", "column", "arch"]
#classes = ["total", "wall", "window",  "door",  "balcony","molding", "deco", "column", "arch","drainpipe","stairs",  "ground surface",
# "terrain",  "roof",  "blinds", "outer ceiling surface", "interior", "other"]
# 0: wall
# 1: window
# 2: door
# 3: molding
# 4: other
# 5: terrain
# 6: column
# 7: arch
class2label = {cls: i for i, cls in enumerate(classes)}
NUM_CLASSES = 8
seg_classes = class2label
seg_label_to_cat = {}
train_ratio = 0.7

for i, cat in enumerate(seg_classes.keys()):
    seg_label_to_cat[i] = cat

def read_las_file_with_labels(file_path):
    las_data = laspy.read(file_path)
    coords = np.vstack((las_data.x, las_data.y, las_data.z)).transpose()
    labels = np.array(las_data.classification, dtype=np.uint8)
    return coords, labels


def inplace_relu(m):
    classname = m.__class__.__name__
    if classname.find('ReLU') != -1:
        m.inplace = True


def parse_args():
    parser = argparse.ArgumentParser('Model')
    parser.add_argument('--model', type=str, default='pointnet_sem_seg', help='model name [default: pointnet_sem_seg]')
    parser.add_argument('--batch_size', type=int, default=16, help='Batch Size during training [default: 16]')
    parser.add_argument('--epoch', default=32, type=int, help='Epoch to run [default: 32]')
    parser.add_argument('--learning_rate', default=0.001, type=float, help='Initial learning rate [default: 0.001]')
    parser.add_argument('--gpu', type=str, default='0', help='GPU to use [default: GPU 0]')
    parser.add_argument('--optimizer', type=str, default='Adam', help='Adam or SGD [default: Adam]')
    parser.add_argument('--log_dir', type=str, default=None, help='Log path [default: None]')
    parser.add_argument('--exp_dir', type=str, default=None, help='Log path [default: None]')
    parser.add_argument('--decay_rate', type=float, default=1e-4, help='weight decay [default: 1e-4]')
    parser.add_argument('--npoint', type=int, default=4096, help='Point Number [default: 4096]')
    parser.add_argument('--step_size', type=int, default=10, help='Decay step for lr decay [default: every 10 epochs]')
    parser.add_argument('--lr_decay', type=float, default=0.7, help='Decay rate for lr decay [default: 0.7]')
    parser.add_argument('--test_area', type=str, default='DEBY_LOD2_4959323.las', help='Which area to use for test, option: 1-6 [default: 5]')
    parser.add_argument('--output_model', type=str, default='/best_model.pth', help='model output name')
    parser.add_argument('--rootdir', type=str, default='/content/drive/MyDrive/ data/tum/tum-facade/training/selected/', help='directory to data')
    parser.add_argument('--visualizeModel', type=str, default=False, help='directory to data')

    return parser.parse_args()


def random_point_cloud_crop(points, num_points):
    assert points.shape[
               0] >= num_points, "Number of points in the point cloud should be greater than or equal to num_points."

    indices = np.random.choice(points.shape[0], num_points, replace=False)
    cropped_points = points[indices]

    return cropped_points


def compute_class_weights(las_dataset):
    # Count the number of points per class
    class_counts = Counter()
    for _, labels in las_dataset:
        class_counts.update(labels)
    # Compute the number of points in the dataset
    num_points = sum(class_counts.values())
    # Compute class weights
    class_weights = {}
    for class_label, count in class_counts.items():
        class_weights[class_label] = num_points / (len(class_counts) * count)
    # Create a list of weights in the same order as class labels
    weight_list = [class_weights[label] for label in sorted(class_weights.keys())]

    return np.array(weight_list, dtype=np.float32)


def PCA(data, correlation=False, sort=True):
    average_data = np.mean(data, axis=0)  # 求 NX3 向量的均值
    decentration_matrix = data - average_data  # 去中心化
    H = np.dot(decentration_matrix.T, decentration_matrix)  # 求解协方差矩阵 H
    eigenvectors, eigenvalues, eigenvectors_T = np.linalg.svd(H)  # SVD求解特征值、特征向量
    # 屏蔽结束

    if sort:
        sort = eigenvalues.argsort()[::-1]  # 降序排列
        eigenvalues = eigenvalues[sort]  # 索引
        eigenvectors = eigenvectors[:, sort]

    return eigenvalues, eigenvectors


def collFeatures(pcd, length, size=0.8):
    pcd_tree = o3d.geometry.KDTreeFlann(pcd)  # set a kd tree for tha point cloud, make searching faster
    normals = []
    llambda = []
    lp = []
    lo = []
    lc = []
    non_idx = []
    # print(point_cloud_o3d)  #geometry::PointCloud with 10000 points.
    print(length)  # 10000
    for i in range(length):
        # search_knn_vector_3d， input[point，x]      returns [int, open3d.utility.IntVector, open3d.utility.DoubleVector]
        [_, idx, _] = pcd_tree.search_radius_vector_3d(pcd.points[i], size)
        # asarray is the same as array  but asarray will save the memeory
        k_nearest_point = np.asarray(pcd.points)[idx,
                          :]  # find the surrounding points for each point, set them as a curve and use PCA to find the normal
        lamb, v = PCA(k_nearest_point)
        if len(k_nearest_point) == 1:
            non_idx.append(i)  # record the index that has no knn point
            p = 0
            o = 0
            c = 0
        else:
            p = (lamb[1] - lamb[2]) / lamb[0]  # calculate features based on eigenvalues
            o = pow(lamb[0] * lamb[1] * lamb[2], 1.0 / 3.0)
            c = lamb[2] / sum(lamb)
        normals.append(v[:, 1])
        llambda.append(lamb)
        lp.append(p)
        lo.append(o)
        lc.append(c)
    return np.array(normals), np.array(llambda), np.array(lp).reshape(length, -1), np.array(lo).reshape(length,-1), np.array(lc).reshape(length, -1), np.array(non_idx)


def downsamplingPCD(pcd, dataset):
    #Downsample
    downpcd = pcd.voxel_down_sample(voxel_size=0.05)
    downsampled_points = np.asarray(downpcd.points)
    downsampled_labels = np.asarray(downpcd.get_point_attr("labels"))

    #Update dataset
    dataset.room_points = [downsampled_points]
    dataset.room_labels = [downsampled_labels]
    dataset.room_idxs = np.array([0])

    #Update pcd after downsampling
    all_points = np.vstack(dataset.room_points)
    all_labels = np.concatenate(dataset.room_labels)
    pcd_update = o3d.geometry.PointCloud(o3ddevice)
    pcd_update.points = o3d.utility.Vector3dVector(all_points)

    return pcd_update, all_points, all_labels, dataset

def createPCD(dataset):
    # Concatenate room_points and room_labels from all rooms
    all_points = np.vstack(dataset.room_points)
    all_labels = np.concatenate(dataset.room_labels)

    # Create an Open3D point cloud object
    pcd = o3d.geometry.PointCloud()

    # Set the point positions using all_points
    pcd.points = o3d.utility.Vector3dVector(all_points)

    # Set the point labels using all_labels
    all_labels_np = all_labels.reshape(-1, 1)  # Reshape the labels to have shape (N, 1)

    # Create a tensor for point labels and assign it to the custom attribute 'labels'
    labels_tensor = o3d.core.Tensor(all_labels_np, dtype=o3d.core.Dtype.Int32)
    pcd.set_point_attr("labels", labels_tensor)

    return pcd, all_points, all_labels


class CustomDataset(Dataset):
    def __init__(self, las_file_list=None, num_classes=8, num_point=4096, block_size=1.0, sample_rate=1.0, transform=None, indices=None):
        super().__init__()
        self.num_point = num_point
        self.block_size = block_size
        self.transform = transform
        self.room_points, self.room_labels = [], []
        self.room_coord_min, self.room_coord_max = [], []
        adjustedclass = num_classes
        range_class = num_classes+1

        #For Geometric Features
        self.eigenNorm = None
        self.llambda = None
        self.lp = None
        self.lo = None
        self.lc = None
        self.non_index = None

        # Use glob to find all .las files in the data_root directory
        las_files = las_file_list
        print(las_file_list)
        rooms = sorted(las_files)
        num_point_all = []
        labelweights = np.zeros(adjustedclass)

        new_class_mapping = {1: 0, 2: 1, 3:2, 6: 3, 13: 4, 11: 5, 7: 6, 8: 7}

        for room_path in rooms:
            # Read LAS file
            print("Reading = " + room_path)
            las_data = laspy.read(room_path)
            coords = np.vstack((las_data.x, las_data.y, las_data.z)).transpose()
            labels = np.array(las_data.classification, dtype=np.uint8)

            # Merge labels as per instructions
            labels[(labels == 5) | (labels == 6)] = 6  # Merge molding and decoration
            labels[(labels == 1) |(labels == 9) | (labels == 15) | (labels == 10)] = 1  # Merge wall, drainpipe, outer ceiling surface, and stairs
            labels[(labels == 12) | (labels == 11)] = 11  # Merge terrain and ground surface
            labels[(labels == 13) | (labels == 16) | (labels == 17)] = 13  # Merge interior, roof, and other
            labels[labels == 14] = 2  # Add blinds to window

            # Map merged labels to new labels (0 to 7)
            labels = np.vectorize(new_class_mapping.get)(labels)

            room_data = np.concatenate((coords, labels[:, np.newaxis]), axis=1)  # xyzl, N*4
            points, labels = room_data[:, 0:3], room_data[:, 3]  # xyz, N*3; l, N
            tmp, _ = np.histogram(labels, range(range_class))
            labelweights += tmp
            coord_min, coord_max = np.amin(points, axis=0), np.amax(points, axis=0)
            self.room_points.append(points)
            self.room_labels.append(labels)
            self.room_coord_min.append(coord_min)
            self.room_coord_max.append(coord_max)
            num_point_all.append(labels.size)

        sample_prob = num_point_all / np.sum(num_point_all)
        num_iter = int(np.sum(num_point_all) * sample_rate / num_point)
        room_idxs = []

        for index in range(len(rooms)):
            room_idxs.extend([index] * int(round(sample_prob[index] * num_iter)))
        self.room_idxs = np.array(room_idxs)

        if indices is not None:
            self.room_idxs = self.room_idxs[indices]

            print("Calcualate Weights")
            # Calculate labelweights for the selected subset
            labelweights = np.zeros(adjustedclass)
            print("len = %f" % len(self.room_idxs))
            for room_idx in self.room_idxs:
                labels = self.room_labels[room_idx]
                tmp, _ = np.histogram(labels, range(range_class))
                labelweights += tmp

        print("wall", "window",  "door",  "molding", "other", "terrain", "column", "arch")
        print(labelweights)
        labelweights = labelweights.astype(np.float32)
        labelweights = labelweights / np.sum(labelweights)
        self.labelweights = np.power(np.amax(labelweights) / labelweights, 1 / 3.0)
        print(self.labelweights)

        print("Totally {} samples in dataset.".format(len(self.room_idxs)))

    def __getitem__(self, idx):
        room_idx = self.room_idxs[idx]
        points = self.room_points[room_idx]   # N * 6
        labels = self.room_labels[room_idx]   # N
        N_points = points.shape[0]
        lp_data = self.room_lp[room_idx]  # N * lp_features
        lo_data = self.room_lo[room_idx]  # N * lo_features
        lc_data = self.room_lc[room_idx]  # N * lc_features

        while (True):
            center = points[np.random.choice(N_points)][:3]
            block_min = center - [self.block_size / 2.0, self.block_size / 2.0, 0]
            block_max = center + [self.block_size / 2.0, self.block_size / 2.0, 0]
            point_idxs = np.where((points[:, 0] >= block_min[0]) & (points[:, 0] <= block_max[0]) & (points[:, 1] >= block_min[1]) & (points[:, 1] <= block_max[1]))[0]
            if point_idxs.size > 1024:
                break

        if point_idxs.size >= self.num_point:
            selected_point_idxs = np.random.choice(point_idxs, self.num_point, replace=False)
        else:
            selected_point_idxs = np.random.choice(point_idxs, self.num_point, replace=True)

        # normalize
        selected_points = points[selected_point_idxs, :]  # num_point * 6
        current_points = np.zeros((self.num_point, 6))  # num_point * 6
        current_points[:, 3] = selected_points[:, 0] / self.room_coord_max[room_idx][0]
        current_points[:, 4] = selected_points[:, 1] / self.room_coord_max[room_idx][1]
        current_points[:, 5] = selected_points[:, 2] / self.room_coord_max[room_idx][2]
        selected_points[:, 0] = selected_points[:, 0] - center[0]
        selected_points[:, 1] = selected_points[:, 1] - center[1]
        current_points[:, 0:3] = selected_points
        current_labels = labels[selected_point_idxs]
        if self.transform is not None:
            current_points, current_labels = self.transform(current_points, current_labels)

        return current_points, current_labels

        # normalize
        selected_points = points[selected_point_idxs, :]  # num_point * 6
        selected_lp = lp_data[selected_point_idxs, :]  # num_point * lp_features
        selected_lo = lo_data[selected_point_idxs, :]  # num_point * lo_features
        selected_lc = lc_data[selected_point_idxs, :]  # num_point * lc_features

        current_points = np.zeros((self.num_point, 6))  # num_point * 6
        current_points[:, 3] = selected_points[:, 0] / self.room_coord_max[room_idx][0]
        current_points[:, 4] = selected_points[:, 1] / self.room_coord_max[room_idx][1]
        current_points[:, 5] = selected_points[:, 2] / self.room_coord_max[room_idx][2]
        selected_points[:, 0] = selected_points[:, 0] - center[0]
        selected_points[:, 1] = selected_points[:, 1] - center[1]
        current_points[:, 0:3] = selected_points

        current_features = np.hstack((current_points, selected_lp, selected_lo, selected_lc))

        current_labels = labels[selected_point_idxs]
        if self.transform is not None:
            current_features, current_labels = self.transform(current_features, current_labels)

        return current_features, current_labels

    def __len__(self):
        return len(self.room_idxs)

    def filtered_indices(self):
        total_indices = set(range(len(self.room_points)))
        non_index_set = set(self.non_index)
        filtered_indices = list(total_indices - non_index_set)
        return filtered_indices

    def filtered_update(self, filtered_indices):
        self.room_points = [self.room_points[i] for i in filtered_indices]
        self.room_labels = [self.room_labels[i] for i in filtered_indices]
        self.room_coord_min = [self.room_coord_min[i] for i in filtered_indices]
        self.room_coord_max = [self.room_coord_max[i] for i in filtered_indices]

        index_mapping = {old_idx: new_idx for new_idx, old_idx in enumerate(filtered_indices)}
        new_room_idxs = [index_mapping[old_idx] for old_idx in self.room_idxs if old_idx in index_mapping]
        self.room_idxs = np.array(new_room_idxs)

    def save_data(self, file_path):
        with h5py.File(file_path, 'w') as f:
            # Save room points, labels, and additional attributes
            for i, (points, labels) in enumerate(zip(self.room_points, self.room_labels)):
                f.create_dataset(f'room_points/{i}', data=points)
                f.create_dataset(f'room_labels/{i}', data=labels)

            # Save filtered room points and labels
            for i, (points, labels) in enumerate(zip(self.filtered_room_points, self.filtered_room_labels)):
                f.create_dataset(f'filtered_room_points/{i}', data=points)
                f.create_dataset(f'filtered_room_labels/{i}', data=labels)

            # Save additional features
            f.create_dataset('eigenNorm', data=self.eigenNorm)
            f.create_dataset('llambda', data=self.llambda)
            f.create_dataset('lp', data=self.lp)
            f.create_dataset('lo', data=self.lo)
            f.create_dataset('lc', data=self.lc)
            f.create_dataset('non_index', data=self.non_index)

    #@staticmethod
    def load_data(file_path):
        dataset = CustomDataset()  # Initialize with default or placeholder parameters
        with h5py.File(file_path, 'r') as f:
            # Load room points, labels, and additional attributes
            dataset.room_points = [f[f'room_points/{i}'][()] for i in range(len(f['room_points']))]
            dataset.room_labels = [f[f'room_labels/{i}'][()] for i in range(len(f['room_labels']))]

            # Load filtered room points and labels
            dataset.filtered_room_points = [f[f'filtered_room_points/{i}'][()] for i in
                                            range(len(f['filtered_room_points']))]
            dataset.filtered_room_labels = [f[f'filtered_room_labels/{i}'][()] for i in
                                            range(len(f['filtered_room_labels']))]

            # Load additional features
            dataset.eigenNorm = f['eigenNorm'][()]
            dataset.llambda = f['llambda'][()]
            dataset.lp = f['lp'][()]
            dataset.lo = f['lo'][()]
            dataset.lc = f['lc'][()]
            dataset.non_index = f['non_index'][()]

        return dataset


def main(args):
    def log_string(str):
        logger.info(str)
        print(str)

    root = args.rootdir
    '''HYPER PARAMETER'''
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    '''CREATE DIR'''
    timestr = str(datetime.datetime.now().strftime('%Y-%m-%d_%H-%M'))
    if args.exp_dir is None:
        experiment_dir = Path('./log/')
    else:
        experiment_dir = Path(args.exp_dir)
        print(experiment_dir)
    experiment_dir.mkdir(exist_ok=True)
    experiment_dir = experiment_dir.joinpath('sem_seg')
    experiment_dir.mkdir(exist_ok=True)
    if args.log_dir is None:
        experiment_dir = experiment_dir.joinpath(timestr)
    else:
        experiment_dir = experiment_dir.joinpath(args.log_dir)
    experiment_dir.mkdir(exist_ok=True)
    checkpoints_dir = experiment_dir.joinpath('checkpoints/')
    checkpoints_dir.mkdir(exist_ok=True)
    log_dir = experiment_dir.joinpath('logs/')
    log_dir.mkdir(exist_ok=True)

    '''LOG'''
    args = parse_args()
    logger = logging.getLogger("Model")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler = logging.FileHandler('%s/%s.txt' % (log_dir, args.model))
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    log_string('PARAMETER ...')
    log_string(args)

    NUM_POINT = args.npoint
    BATCH_SIZE = args.batch_size

    las_file_list = [file for file in glob.glob(root + '/*.las') if not file.endswith(args.test_area )]


    lidar_dataset = CustomDataset(las_file_list, num_classes=NUM_CLASSES, num_point=NUM_POINT, transform=None)
    print("Dataset taken")
    # Split the dataset into training and evaluation sets
    train_size = int(train_ratio * len(lidar_dataset))
    test_size = len(lidar_dataset) - train_size

       # Split the full dataset into train and test sets
    train_indices, test_indices = random_split(range(len(lidar_dataset)), [train_size, test_size])

    #print("start loading training data ...")
    #TRAIN_DATASET = CustomDataset(las_file_list, num_classes=NUM_CLASSES, num_point=NUM_POINT, transform=None, indices=train_indices)
    #custom_dataset.save_data('custom_dataset.pkl')
    #new_custom_dataset.load_data('custom_dataset.pkl')
    #print("start loading test data ...")
    #TEST_DATASET = CustomDataset(las_file_list, num_classes=NUM_CLASSES, num_point=NUM_POINT, transform=None, indices=test_indices)

    print("start loading training data ...")
    TRAIN_DATASET = CustomDataset(las_file_list, num_classes=NUM_CLASSES, num_point=NUM_POINT, transform=None)

    print("room_idx training")
    print(TRAIN_DATASET.room_idxs)
    print(len(TRAIN_DATASET))

    #Open3D
    pcd_train, train_points, train_labels = createPCD(TRAIN_DATASET)

    #Downsampling
    #pcd_train, train_points, train_labels, TRAIN_DATASET = downsamplingPCD(pcd_train, TRAIN_DATASET)
    print("downsampled room_idx training")
    print(TRAIN_DATASET.room_idxs)

    # Visualization
    if args.visualizeModel is True:
        colors = plt.get_cmap("tab20")(np.array(train_labels).reshape(-1) / 17.0)
        colors = colors[:, 0:3]
        pcd_train.colors = o3d.utility.Vector3dVector(colors)
        o3d.visualization.draw_geometries([pcd_train], window_name='test the color', width=800, height=600)

    #Geometric Feature Addition
    # add features, normals, lambda, p, o, c, radius is 0.8m
    train_total_len = len(TRAIN_DATASET)
    eigenNorm, llambda, lp, lo, lc, non_index = collFeatures(pcd_train, train_total_len)


    print("eigenvector len = %" %len(eigenNorm))
    print("non-index = %" %len(non_index))

    # Store the additional features in the CustomDataset instance
    TRAIN_DATASET.eigenNorm = eigenNorm
    TRAIN_DATASET.llambda = llambda
    TRAIN_DATASET.lp = lp
    TRAIN_DATASET.lo = lo
    TRAIN_DATASET.lc = lc
    TRAIN_DATASET.non_index = non_index

    # Filter the points and labels using the non_index variable
    if len(non_index) != 0:
        filtered_indices = TRAIN_DATASET.filtered_indices()
        TRAIN_DATASET.filtered_update(filtered_indices)

    print("geometric room_idx training")
    print(TRAIN_DATASET.room_idxs)
    print(len(TRAIN_DATASET))

    TrainTime = time.time()
    timetaken = TrainTime-start
    sec = timetaken%60
    t1 = timetaken/60
    mint = t1%60
    hour = t1/60

    print("Time taken to load Training = %i:%i:%i" % (hour, mint, sec))


    # Evaluation DATASET
    print("start loading evalaution data ...")
    TEST_DATASET = CustomDataset(las_file_list2, num_classes=NUM_CLASSES, num_point=NUM_POINT, transform=None) #Evaluation


    print("room_idx evaluation")
    print(TEST_DATASET.room_idxs)
    print(len(TEST_DATASET))

    #Open3D
    pcd_test, test_points, test_labels = createPCD(TEST_DATASET)

    #Downsampling
    #pcd_test, test_points, test_labels, TRAIN_DATASET = downsamplingPCD(pcd_test, TRAIN_DATASET)
    print("downsampled room_idx evaluation")
    print(TEST_DATASET.room_idxs)

    # Visualization
    if args.visualizeModel is True:
        colors = plt.get_cmap("tab20")(np.array(test_labels).reshape(-1) / 17.0)
        colors = colors[:, 0:3]
        pcd_test.colors = o3d.utility.Vector3dVector(colors)
        o3d.visualization.draw_geometries([pcd_test], window_name='test the color', width=800, height=600)

    #Geometric Feature Addition
    # add features, normals, lambda, p, o, c, radius is 0.8m
    test_total_len = len(TEST_DATASET)
    eigenNorm, llambda, lp, lo, lc, non_index = collFeatures(pcd_test, test_total_len)

    print("eigenvector len = %" %len(eigenNorm))
    print("non-index = %" %len(non_index))

    # Store the additional features in the CustomDataset instance
    TEST_DATASET.eigenNorm = eigenNorm
    TEST_DATASET.llambda = llambda
    TEST_DATASET.lp = lp
    TEST_DATASET.lo = lo
    TEST_DATASET.lc = lc
    TEST_DATASET.non_index = non_index

    # Filter the points and labels using the non_index variable
    if len(non_index) != 0:
        filtered_indices = TEST_DATASET.filtered_indices()
        TEST_DATASET.filtered_update(filtered_indices)

    print("geometric room_idx evaluation")
    print(TEST_DATASET.room_idxs)
    print(len(TEST_DATASET))

    TestTime = time.time()
    timetaken = TestTime-start
    sec = timetaken%60
    t1 = timetaken/60
    mint = t1%60
    hour = t1/60

    print("Time taken to load Evaluation = %i:%i:%i" % (hour, mint, sec))


    trainDataLoader = DataLoader(TRAIN_DATASET, batch_size=BATCH_SIZE, shuffle=True, num_workers=10,
                                                  pin_memory=True, drop_last=True,
                                                  worker_init_fn=lambda x: np.random.seed(x + int(time.time())))
    testDataLoader = DataLoader(TEST_DATASET, batch_size=BATCH_SIZE, shuffle=False, num_workers=10,
                                                 pin_memory=True, drop_last=True)

    weights = torch.Tensor(TRAIN_DATASET.labelweights).cuda()

    log_string("The number of training data is: %d" % len(TRAIN_DATASET))
    log_string("The number of test data is: %d" % len(TEST_DATASET))

    print("Length of the dataset:", len(TRAIN_DATASET))
    print("Length of the trainDataLoader:", len(trainDataLoader))

    '''MODEL LOADING'''
    MODEL = importlib.import_module(args.model)
    shutil.copy('models/%s.py' % args.model, str(experiment_dir))
    shutil.copy('models/pointnet2_utils.py', str(experiment_dir))

    classifier = MODEL.get_model(NUM_CLASSES).cuda()
    criterion = MODEL.get_loss().cuda()
    classifier.apply(inplace_relu)

    def weights_init(m):
        classname = m.__class__.__name__
        if classname.find('Conv2d') != -1:
            torch.nn.init.xavier_normal_(m.weight.data)
            torch.nn.init.constant_(m.bias.data, 0.0)
        elif classname.find('Linear') != -1:
            torch.nn.init.xavier_normal_(m.weight.data)
            torch.nn.init.constant_(m.bias.data, 0.0)

    model_name = args.output_model


    try:
        checkpoint = torch.load(str(experiment_dir) + '/checkpoints'+model_name)
        start_epoch = checkpoint['epoch']
        classifier.load_state_dict(checkpoint['model_state_dict'])
        log_string('Use pretrain model')
    except:
        log_string('No existing model, starting training from scratch...')
        start_epoch = 0
        classifier = classifier.apply(weights_init)

    if args.optimizer == 'Adam':
        optimizer = torch.optim.Adam(
            classifier.parameters(),
            lr=args.learning_rate,
            betas=(0.9, 0.999),
            eps=1e-08,
            weight_decay=args.decay_rate
        )
    else:
        optimizer = torch.optim.SGD(classifier.parameters(), lr=args.learning_rate, momentum=0.9)

    def bn_momentum_adjust(m, momentum):
        if isinstance(m, torch.nn.BatchNorm2d) or isinstance(m, torch.nn.BatchNorm1d):
            m.momentum = momentum

    LEARNING_RATE_CLIP = 1e-5
    MOMENTUM_ORIGINAL = 0.1
    MOMENTUM_DECCAY = 0.5
    MOMENTUM_DECCAY_STEP = args.step_size

    global_epoch = 0
    best_iou = 0

    midpt = time.time()
    timetaken = midpt-start
    sec = timetaken%60
    t1 = timetaken/60
    mint = t1%60
    hour = t1/60

    print("Time taken to prepare = %i:%i:%i" % (hour, mint, sec))

    print("Identified Weights")
    print(weights)

    accuracyChart = []

    for epoch in range(start_epoch, args.epoch):
        '''Train on chopped scenes'''
        log_string('**** Epoch %d (%d/%s) ****' % (global_epoch + 1, epoch + 1, args.epoch))
        lr = max(args.learning_rate * (args.lr_decay ** (epoch // args.step_size)), LEARNING_RATE_CLIP)
        log_string('Learning rate:%f' % lr)
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
        momentum = MOMENTUM_ORIGINAL * (MOMENTUM_DECCAY ** (epoch // MOMENTUM_DECCAY_STEP))
        if momentum < 0.01:
            momentum = 0.01
        print('BN momentum updated to: %f' % momentum)
        classifier = classifier.apply(lambda x: bn_momentum_adjust(x, momentum))
        num_batches = len(trainDataLoader)
        total_correct = 0
        total_seen = 0
        loss_sum = 0
        classifier = classifier.train()

        for i, (points, target) in tqdm(enumerate(trainDataLoader), total=len(trainDataLoader), smoothing=0.9):
            optimizer.zero_grad()

            points = points.data.numpy()
            points[:, :, :3] = provider.rotate_point_cloud_z(points[:, :, :3])
            points = torch.Tensor(points)
            points, target = points.float().cuda(), target.long().cuda()
            points = points.transpose(2, 1)

            seg_pred, trans_feat = classifier(points)
            seg_pred = seg_pred.contiguous().view(-1, NUM_CLASSES)

            batch_label = target.view(-1, 1)[:, 0].cpu().data.numpy()
            target = target.view(-1, 1)[:, 0]
            loss = criterion(seg_pred, target, trans_feat, weights)
            loss.backward()
            optimizer.step()

            pred_choice = seg_pred.cpu().data.max(1)[1].numpy()
            correct = np.sum(pred_choice == batch_label)
            total_correct += correct
            total_seen += (BATCH_SIZE * NUM_POINT)
            loss_sum += loss
        print("loss value = %f" % loss_sum)
        log_string('Training mean loss: %f' % (loss_sum / num_batches))
        log_string('Training accuracy: %f' % (total_correct / float(total_seen)))

        if epoch % 5 == 0:
            logger.info('Save model...')
            savepath = str(checkpoints_dir) + '/model.pth'
            log_string('Saving at %s' % savepath)
            state = {
                'epoch': epoch,
                'model_state_dict': classifier.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
            }
            torch.save(state, savepath)
            log_string('Saving model....')

        '''Evaluate on chopped scenes'''
        with torch.no_grad():
            num_batches = len(testDataLoader)
            total_correct = 0
            total_seen = 0
            loss_sum = 0
            labelweights = np.zeros(NUM_CLASSES)
            total_seen_class = [0 for _ in range(NUM_CLASSES)]
            total_correct_class = [0 for _ in range(NUM_CLASSES)]
            total_iou_deno_class = [0 for _ in range(NUM_CLASSES)]
            classifier = classifier.eval()

            log_string('---- EPOCH %03d EVALUATION ----' % (global_epoch + 1))
            for i, (points, target) in tqdm(enumerate(testDataLoader), total=len(testDataLoader), smoothing=0.9):
                points = points.data.numpy()
                points = torch.Tensor(points)
                #print("Batch shape:", points.shape)  # Debug
                points, target = points.float().cuda(), target.long().cuda()
                points = points.transpose(2, 1)

                seg_pred, trans_feat = classifier(points)
                pred_val = seg_pred.contiguous().cpu().data.numpy()
                seg_pred = seg_pred.contiguous().view(-1, NUM_CLASSES)

                batch_label = target.cpu().data.numpy()
                target = target.view(-1, 1)[:, 0]
                loss = criterion(seg_pred, target, trans_feat, weights)
                loss_sum += loss
                pred_val = np.argmax(pred_val, 2)
                correct = np.sum((pred_val == batch_label))
                total_correct += correct
                total_seen += (BATCH_SIZE * NUM_POINT)
                tmp, _ = np.histogram(batch_label, range(NUM_CLASSES + 1))
                labelweights += tmp

                for l in range(NUM_CLASSES):
                    total_seen_class[l] += np.sum((batch_label == l))
                    total_correct_class[l] += np.sum((pred_val == l) & (batch_label == l))
                    total_iou_deno_class[l] += np.sum(((pred_val == l) | (batch_label == l)))

            labelweights = labelweights.astype(np.float32) / np.sum(labelweights.astype(np.float32))
            mIoU = np.mean(np.array(total_correct_class) / (np.array(total_iou_deno_class, dtype=np.float) + 1e-6))
            log_string('eval mean loss: %f' % (loss_sum / float(num_batches)))
            log_string('eval point avg class IoU: %f' % (mIoU))
            log_string('eval point accuracy: %f' % (total_correct / float(total_seen)))
            log_string('eval point avg class acc: %f' % (np.mean(np.array(total_correct_class) / (np.array(total_seen_class, dtype=np.float) + 1e-6))))


            iou_per_class_str = '------- IoU --------\n'
            for l in range(NUM_CLASSES):
                denom = float(total_iou_deno_class[l])

                if denom == 0:
                  tmp = denom
                else:
                  tmp = total_correct_class[l] / float(total_iou_deno_class[l])

                iou_per_class_str += 'class %s weight: %.3f, IoU: %.3f \n' % (
                    seg_label_to_cat[l] 
                    + ' ' * (14 - len(seg_label_to_cat[l])), 
                    labelweights[l - 1], tmp)

            log_string(iou_per_class_str)
            log_string('Eval mean loss: %f' % (loss_sum / num_batches))
            log_string('Eval accuracy: %f' % (total_correct / float(total_seen)))

            tmpVal = (total_correct / float(total_seen))
            accuracyChart.append(tmpVal)

            if mIoU >= best_iou:
                best_iou = mIoU
                logger.info('Save model...')
                savepath = str(checkpoints_dir) + model_name
                log_string('Saving at %s' % savepath)
                state = {
                    'epoch': epoch,
                    'class_avg_iou': mIoU,
                    'model_state_dict': classifier.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                }
                torch.save(state, savepath)
                log_string('Saving model....')
            log_string('Best mIoU: %f' % best_iou)
        global_epoch += 1

    return accuracyChart




if __name__ == '__main__':
    args = parse_args()
    start = time.time()
    accuracyChart = main(args)

    max_value = max(accuracyChart)
    max_index = accuracyChart.index(max_value)

    print(max_index)
    end = time.time()
    timetaken = end-start
    sec = timetaken%60
    t1 = timetaken/60
    mint = t1%60
    hour = t1/60

    print("Time taken = %i:%i:%i" % (hour, mint, sec))