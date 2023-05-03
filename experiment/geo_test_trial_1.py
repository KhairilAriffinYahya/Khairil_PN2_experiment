import argparse
import os
import torch
import logging
from pathlib import Path
import sys
import importlib
from tqdm import tqdm
import laspy
import glob
import numpy as np
import open3d as o3d
import pickle
import h5py
from models.localfunctions import timePrint, CurrentTime
import pytz
from geofunction import PCA, collFeatures, downsamplingPCD, createPCD
import matplotlib.pyplot as plt

'''Adjust permanent/file/static variables here'''

timezone = pytz.timezone('Asia/Singapore')
print("Check current time")
CurrentTime(timezone)
saveTest = "geo_testdata.pkl"
saveDir = "/content/Khairil_PN2_experiment/experiment/data/saved_data/"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 0: wall, # 1: window, # 2: door, # 3: molding, # 4: other, # 5: terrain, # 6: column, # 7: arch
classes = ["wall", "window",  "door",  "molding", "other", "terrain", "column", "arch"]
NUM_CLASSES = 8
train_ratio = 0.7

''''''

sys.path.append(os.path.join(BASE_DIR, 'models'))
class2label = {cls: i for i, cls in enumerate(classes)}
seg_classes = class2label
seg_label_to_cat = {}
for i, cat in enumerate(seg_classes.keys()):
    seg_label_to_cat[i] = cat

print(seg_label_to_cat)

# Adjust parameters here if there no changes to reduce line

def parse_args():
    '''PARAMETERS'''
    parser = argparse.ArgumentParser('Model')
    parser.add_argument('--batch_size', type=int, default=32, help='batch size in testing [default: 32]')
    parser.add_argument('--gpu', type=str, default='0', help='specify gpu device')
    parser.add_argument('--num_point', type=int, default=4096, help='point number [default: 4096]')
    parser.add_argument('--log_dir', type=str, required=True, help='experiment root')
    parser.add_argument('--exp_dir', type=str, default=None, help='Log path [default: None]')
    parser.add_argument('--visual', action='store_true', default=False, help='visualize result [default: False]')
    parser.add_argument('--test_area', type=str, default='cc_o_DEBY_LOD2_4959323.las', help='area for testing, option: 1-6 [default: 5]')
    parser.add_argument('--num_votes', type=int, default=5, help='aggregate segmentation scores with voting [default: 5]')
    parser.add_argument('--model', type=str, default='pointnet2_sem_seg_geo_trial', help='model name [default: pointnet_sem_seg]')
    parser.add_argument('--output_model', type=str, default='/best_model.pth', help='model output name')
    parser.add_argument('--rootdir', type=str, default='/content/drive/MyDrive/ data/tum/tum-facade/training/cc_selected/', help='directory to data')
    parser.add_argument('--load', type=bool, default=False, help='load saved data or new')
    parser.add_argument('--save', type=bool, default=False, help='save data')
    parser.add_argument('--visualizeModel', type=str, default=False, help='directory to data')
    parser.add_argument('--downsample', type=bool, default=False, help='downsample data')
    parser.add_argument('--calculate_geometry', type=bool, default=False, help='decide where to calculate geometry')
    parser.add_argument('--geometry_features', type=array, default=['p','o','c'], help='select which geometry_features to add')


    return parser.parse_args()


def add_vote(vote_label_pool, point_idx, pred_label, weight):
    B = pred_label.shape[0]
    N = pred_label.shape[1]
    for b in range(B):
        for n in range(N):
            if weight[b, n] != 0 and not np.isinf(weight[b, n]):
                vote_label_pool[int(point_idx[b, n]), int(pred_label[b, n])] += 1
    return vote_label_pool


class TestCustomDataset():
    # prepare to give prediction on each points
    def __init__(self, root, las_file_list='trainval_fullarea', num_classes=8, block_points=4096, stride=0.5,
                 block_size=1.0, padding=0.001):
        self.block_points = block_points
        self.block_size = block_size
        self.padding = padding
        self.file_list = las_file_list
        self.stride = stride
        self.scene_points_num = []

        #For Geometric Features
        self.lp = []
        self.lo = []
        self.lc = []
        self.non_index = []


        # Return early if las_file_list is None
        if las_file_list is None:
            self.room_idxs = np.array([])
            return
            
        adjustedclass = num_classes
        range_class = adjustedclass + 1

        self.scene_points_list = []
        self.semantic_labels_list = []
        self.room_coord_min, self.room_coord_max = [], []

        new_class_mapping = {1: 0, 2: 1, 3: 2, 6: 3, 13: 4, 11: 5, 7: 6, 8: 7}

        for files in self.file_list:
            file_path = os.path.join(root, files)
            in_file = laspy.read(file_path)
            points = np.vstack((in_file.x, in_file.y, in_file.z)).T
            labels = np.array(in_file.classification, dtype=np.int32)
            print("Labels")
            print(labels)
            if calculate_geometry is False:
                if 'p' in args.geometry_features:
                    tmp_p = np.array(las_data.planarity, dtype=np.uint8)
                    self.lp.append(tmp_p)
                if 'o' in args.geometry_features:
                    tmp_p = np.array(las_data.omnivariance, dtype=np.uint8)
                    self.lo.append(tmp_o)
                if 'c' in args.geometry_features:
                    tmp_p = np.array(las_data.surface_variation, dtype=np.uint8)
                    self.lc.append(tmp_c)
                    
            # Merge labels as per instructions
            labels[(labels == 5) | (labels == 6)] = 6  # Merge molding and decoration
            labels[(labels == 1) | (labels == 9) | (labels == 15) | (
                        labels == 10)] = 1  # Merge wall, drainpipe, outer ceiling surface, and stairs
            labels[(labels == 12) | (labels == 11)] = 11  # Merge terrain and ground surface
            labels[(labels == 13) | (labels == 16) | (labels == 17)] = 13  # Merge interior, roof, and other
            labels[labels == 14] = 2  # Add blinds to window

            # Map merged labels to new labels (0 to 7)
            labels = np.vectorize(new_class_mapping.get)(labels)

            data = np.hstack((points, labels.reshape((-1, 1))))
            self.scene_points_list.append(data[:, :3])
            self.semantic_labels_list.append(data[:, 3])
            coord_min, coord_max = np.amin(points, axis=0)[:3], np.amax(points, axis=0)[:3]
            self.room_coord_min.append(coord_min), self.room_coord_max.append(coord_max)
        assert len(self.scene_points_list) == len(self.semantic_labels_list)

    def __getitem__(self, index):
        point_set_ini = self.scene_points_list[index]
        points = point_set_ini[:, :3]
        labels = self.semantic_labels_list[index]
        lp = self.lp  # Load the lp features
        lo = self.lo  # Load the lo features
        lc = self.lc  # Load the lc features
        coord_min, coord_max = np.amin(points, axis=0)[:3], np.amax(points, axis=0)[:3]
        grid_x = int(np.ceil(float(coord_max[0] - coord_min[0] - self.block_size) / self.stride) + 1)
        grid_y = int(np.ceil(float(coord_max[1] - coord_min[1] - self.block_size) / self.stride) + 1)
        data_room, label_room, sample_weight, index_room = np.array([]), np.array([]), np.array([]), np.array([])

        for index_y in range(grid_y):
            for index_x in range(grid_x):
                s_x = coord_min[0] + index_x * self.stride
                e_x = min(s_x + self.block_size, coord_max[0])
                s_x = e_x - self.block_size
                s_y = coord_min[1] + index_y * self.stride
                e_y = min(s_y + self.block_size, coord_max[1])
                s_y = e_y - self.block_size
                point_idxs = np.where((points[:, 0] >= s_x - self.padding) & (points[:, 0] <= e_x + self.padding) &
                                      (points[:, 1] >= s_y - self.padding) & (points[:, 1] <= e_y + self.padding))[0]
                if point_idxs.size == 0:
                    continue

                num_batch = int(np.ceil(point_idxs.size / self.block_points))
                point_size = int(num_batch * self.block_points)
                replace = False if (point_size - point_idxs.size <= point_idxs.size) else True
                point_idxs_repeat = np.random.choice(point_idxs, point_size - point_idxs.size, replace=replace)
                point_idxs = np.concatenate((point_idxs, point_idxs_repeat))
                np.random.shuffle(point_idxs)
                data_batch = points[point_idxs, :]
                normlized_xyz = np.zeros((point_size, 3))
                normlized_xyz[:, 0] = data_batch[:, 0] / coord_max[0]
                normlized_xyz[:, 1] = data_batch[:, 1] / coord_max[1]
                normlized_xyz[:, 2] = data_batch[:, 2] / coord_max[2]
                data_batch[:, 0] = data_batch[:, 0] - (s_x + self.block_size / 2.0)
                data_batch[:, 1] = data_batch[:, 1] - (s_y + self.block_size / 2.0)
                tmp_geo_feature = []
                if 'p' in args.geometry_features:
                    tmp_geo_feature.append(lp[point_idxs, :])
                if 'o' in args.geometry_features:
                    tmp_geo_feature.append(lo[point_idxs, :])
                if 'c' in args.geometry_features:
                    tmp_geo_feature.append(lc[point_idxs, :])
                
                data_batch = np.concatenate((data_batch, lo[point_idxs, :]),axis=1)
                print(data_batch)
                if len(tmp_geo_feature) > 0:
                    geo_feature = np.concatenate(tmp_geo_feature, axis=1)
                    data_batch = np.concatenate((data_batch, geo_feature), axis=1)
                    print(data_batch)

                label_batch = labels[point_idxs].astype(int)
                batch_weight = self.labelweights[label_batch]

                data_room = np.vstack([data_room, data_batch]) if data_room.size else data_batch
                label_room = np.hstack([label_room, label_batch]) if label_room.size else label_batch
                sample_weight = np.hstack([sample_weight, batch_weight]) if label_room.size else batch_weight
                index_room = np.hstack([index_room, point_idxs]) if index_room.size else point_idxs

        data_room = data_room.reshape((-1, self.block_points, data_room.shape[1]))
        label_room = label_room.reshape((-1, self.block_points))
        sample_weight = sample_weight.reshape((-1, self.block_points))
        index_room = index_room.reshape((-1, self.block_points))

        return data_room, label_room, sample_weight, index_room

    def __len__(self):
        return len(self.scene_points_list)


    def filtered_indices(self):
        total_indices = set(range(len(self.room_points)))
        non_index_set = set(self.non_index)
        filtered_indices = list(total_indices - non_index_set)
        return filtered_indices

    def index_update(self, newIndices):
        self.room_idxs = new_room_idxs

    def copy(self, new_indices=None):
        new_dataset = TestCustomDataset()
        new_dataset.block_points = self.block_points
        new_dataset.block_size = self.block_size
        new_dataset.padding = self.padding
        new_dataset.file_list = self.file_list
        new_dataset.stride = self.stride
        new_dataset.num_classes = self.num_classes
        new_dataset.room_coord_min = self.room_coord_min
        new_dataset.room_coord_max = self.room_coord_max
        new_dataset.lp = self.lp
        new_dataset.lo = self.lo
        new_dataset.lc = self.lc
        new_dataset.non_index = self.non_index
        
        new_dataset.scene_points_list = [self.scene_points_list[i] for i in new_indices]
        new_dataset.semantic_labels_list = [self.semantic_labels_list[i] for i in new_indices]

        assert len(new_dataset.scene_points_list) == len(new_dataset.semantic_labels_list)

        return new_dataset



    def calculate_labelweights(self):
        print("Calculate Weights")
        num_classes = self.num_classes
        labelweights = np.zeros(num_classes)
        tmp_scene_points_num = []
        for seg in self.semantic_labels_list:
            tmp, _ = np.histogram(seg, range(num_classes + 1))
            tmp_scene_points_num.append(seg.shape[0])
            labelweights += tmp

        print(labelweights)
        labelweights = labelweights.astype(np.float32)
        labelweights = labelweights / np.sum(labelweights)  # normalize weights to 1
        labelweights = np.power(np.amax(labelweights) / labelweights, 1 / 3.0)  # balance weights

        print(labelweights)
        assert len(labelweights) == num_classes

        return labelweights, tmp_scene_points_num

    def save_data(self, file_path):
        with open(file_path, 'wb') as f:
            pickle.dump(self, f)

    @staticmethod
    def load_data(file_path):
        with open(file_path, 'rb') as f:
            dataset = pickle.load(f)
        return dataset



def main(args):
    def log_string(str):
        logger.info(str)
        print(str)


    '''Initialize'''
    root = args.rootdir
    BATCH_SIZE = args.batch_size
    NUM_POINT = args.num_point
    savetest_path = saveDir+saveTest
    test_file = glob.glob(root + args.test_area )
    
    '''HYPER PARAMETER'''
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    if args.exp_dir is None:
        tmp_dir = 'log/sem_seg/'
    else:
        tmp_dir = args.exp_dir
        print(tmp_dir)
    experiment_dir = tmp_dir + args.log_dir
    visual_dir = experiment_dir + '/visual/'
    visual_dir = Path(visual_dir)
    visual_dir.mkdir(exist_ok=True)

    '''LOG'''
    args = parse_args()
    logger = logging.getLogger("Model")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler = logging.FileHandler('%s/eval.txt' % experiment_dir)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    log_string('PARAMETER ...')
    log_string(args)

    
    '''Dataset'''
    testdatatime = time.time()

    if args.load is False:
        print("start loading test data ...")
        TEST_DATASET_WHOLE_SCENE = TestCustomDataset(root, test_file, num_classes=NUM_CLASSES, block_points=NUM_POINT)
        log_string("The number of test data is: %d" % len(TEST_DATASET_WHOLE_SCENE))

        print("room_idx evaluation")
        print(TEST_DATASET_WHOLE_SCENE.room_idxs)
        print(len(TEST_DATASET_WHOLE_SCENE))

        if calculate_geometry is True:
            #Open3D
            pcd_test, test_points, test_labels = createPCD(TEST_DATASET_WHOLE_SCENE)

            #Downsampling
            if args.downsample is True:
                pcd_test, test_points, test_labels, TRAIN_DATASET = downsamplingPCD(pcd_test, TRAIN_DATASET)
                print("downsampled room_idx evaluation")
                print(TEST_DATASET_WHOLE_SCENE.room_idxs)

            # Visualization
            if args.visualizeModel is True:
                colors = plt.get_cmap("tab20")(np.array(test_labels).reshape(-1) / 17.0)
                colors = colors[:, 0:3]
                pcd_test.colors = o3d.utility.Vector3dVector(colors)
                o3d.visualization.draw_geometries([pcd_test], window_name='test the color', width=800, height=600)

            #Geometric Feature Addition
            # add features, normals, lambda, p, o, c, radius is 0.8m
            test_total_len = len(TEST_DATASET_WHOLE_SCENE)
            eigenNorm, llambda, lp, lo, lc, non_index = collFeatures(pcd_test, test_total_len)

            print("eigenvector len = %" %len(eigenNorm))
            print("non-index = %" %len(non_index))

            # Store the additional features in the CustomDataset instance
            TEST_DATASET_WHOLE_SCENE.lp = lp
            TEST_DATASET_WHOLE_SCENE.lo = lo
            TEST_DATASET_WHOLE_SCENE.lc = lc
            TEST_DATASET_WHOLE_SCENE.non_index = non_index

            # Filter the points and labels using the non_index variable
            if len(non_index) != 0:
                filtered_indices = TEST_DATASET.filtered_indices()
                TEST_DATASET_WHOLE_SCENE.filtered_update(filtered_indices)

            print("geometric room_idx evaluation")
            print(TEST_DATASET_WHOLE_SCENE.room_idxs)
            print(len(TEST_DATASET_WHOLE_SCENE))
    else:
        TEST_DATASET_WHOLE_SCENE = TestCustomDataset.load_data(saveDir+saveTest)

    timePrint(testdatatime)
    CurrentTime(timezone)

    if args.save is True:
        print("Save Test dataset")
        savetesttime = time.time()
        TEST_DATASET_WHOLE_SCENE.save(saveDir+saveTest)
        timePrint(savetesttime)
        CurrentTime(timezone)
        
    '''MODEL LOADING'''
    model_name = args.output_model
    tmp_model = args.model
    if tmp_model == None:
        model_dir = os.listdir(experiment_dir + '/logs')[0].split('.')[0]
    else:
        model_dir = tmp_model
    print(model_dir)
    MODEL = importlib.import_module(model_dir)
    classifier = MODEL.get_model(NUM_CLASSES).cuda()
    checkpoint = torch.load(str(experiment_dir) + '/checkpoints'+model_name)
    classifier.load_state_dict(checkpoint['model_state_dict'])
    classifier = classifier.eval()

    with torch.no_grad():
        scene_id = TEST_DATASET_WHOLE_SCENE.file_list
        scene_id = [x[:-4] for x in scene_id]
        num_batches = len(TEST_DATASET_WHOLE_SCENE)

        total_seen_class = [0 for _ in range(NUM_CLASSES)]
        total_correct_class = [0 for _ in range(NUM_CLASSES)]
        total_iou_deno_class = [0 for _ in range(NUM_CLASSES)]

        log_string('---- EVALUATION WHOLE SCENE----')

        for batch_idx in range(num_batches):
            print("Inference [%d/%d] %s ..." % (batch_idx + 1, num_batches, scene_id[batch_idx]))
            total_seen_class_tmp = [0 for _ in range(NUM_CLASSES)]
            total_correct_class_tmp = [0 for _ in range(NUM_CLASSES)]
            total_iou_deno_class_tmp = [0 for _ in range(NUM_CLASSES)]
            if args.visual:
                fout = open(os.path.join(visual_dir, scene_id[batch_idx] + '_pred.obj'), 'w')
                fout_gt = open(os.path.join(visual_dir, scene_id[batch_idx] + '_gt.obj'), 'w')
            whole_scene_data = TEST_DATASET_WHOLE_SCENE.scene_points_list[batch_idx]
            whole_scene_label = TEST_DATASET_WHOLE_SCENE.semantic_labels_list[batch_idx]
            vote_label_pool = np.zeros((whole_scene_label.shape[0], NUM_CLASSES))
            for _ in tqdm(range(args.num_votes), total=args.num_votes):
                scene_data, scene_label, scene_smpw, scene_point_index = TEST_DATASET_WHOLE_SCENE[batch_idx]
                num_blocks = scene_data.shape[0]
                s_batch_num = (num_blocks + BATCH_SIZE - 1) // BATCH_SIZE
                batch_data = np.zeros((BATCH_SIZE, NUM_POINT, 6))  # Change to 6 (from 9) as there's no color

                batch_label = np.zeros((BATCH_SIZE, NUM_POINT))
                batch_point_index = np.zeros((BATCH_SIZE, NUM_POINT))
                batch_smpw = np.zeros((BATCH_SIZE, NUM_POINT))

                for sbatch in range(s_batch_num):
                    start_idx = sbatch * BATCH_SIZE
                    end_idx = min((sbatch + 1) * BATCH_SIZE, num_blocks)
                    real_batch_size = end_idx - start_idx
                    batch_data[0:real_batch_size, ...] = scene_data[start_idx:end_idx, ...]
                    batch_label[0:real_batch_size, ...] = scene_label[start_idx:end_idx, ...]
                    batch_point_index[0:real_batch_size, ...] = scene_point_index[start_idx:end_idx, ...]
                    batch_smpw[0:real_batch_size, ...] = scene_smpw[start_idx:end_idx, ...]

                    torch_data = torch.Tensor(batch_data)
                    torch_data = torch_data.float().cuda()
                    torch_data = torch_data.transpose(2, 1)
                    seg_pred, _ = classifier(torch_data)
                    batch_pred_label = seg_pred.contiguous().cpu().data.max(2)[1].numpy()

                    vote_label_pool = add_vote(vote_label_pool, batch_point_index[0:real_batch_size, ...],
                                               batch_pred_label[0:real_batch_size, ...],
                                               batch_smpw[0:real_batch_size, ...])

            pred_label = np.argmax(vote_label_pool, 1)

            for l in range(NUM_CLASSES):
                total_seen_class_tmp[l] += np.sum((whole_scene_label == l))
                total_correct_class_tmp[l] += np.sum((pred_label == l) & (whole_scene_label == l))
                total_iou_deno_class_tmp[l] += np.sum(((pred_label == l) | (whole_scene_label == l)))
                total_seen_class[l] += total_seen_class_tmp[l]
                total_correct_class[l] += total_correct_class_tmp[l]
                total_iou_deno_class[l] += total_iou_deno_class_tmp[l]

            iou_map = np.array(total_correct_class_tmp) / (np.array(total_iou_deno_class_tmp, dtype=float) + 1e-6)
            print(iou_map)
            arr = np.array(total_seen_class_tmp)
            tmp_iou = np.mean(iou_map[arr != 0])
            log_string('Mean IoU of %s: %.4f' % (scene_id[batch_idx], tmp_iou))
            print('----------------------------')

            filename = os.path.join(visual_dir, scene_id[batch_idx] + '.txt')
            with open(filename, 'w') as pl_save:
                for i in pred_label:
                    pl_save.write(str(int(i)) + '\n')
                pl_save.close()

            if args.visual:
                for i in range(whole_scene_label.shape[0]):
                    fout.write('v %f %f %f\n' % (
                        whole_scene_data[i, 0], whole_scene_data[i, 1], whole_scene_data[i, 2]))
                    fout_gt.write('v %f %f %f\n' % (
                        whole_scene_data[i, 0], whole_scene_data[i, 1], whole_scene_data[i, 2]))

            if args.visual:
                fout.close()
                fout_gt.close()

        IoU = np.array(total_correct_class) / (np.array(total_iou_deno_class, dtype=np.float) + 1e-6)
        iou_per_class_str = '------- IoU --------\n'
        for l in range(NUM_CLASSES):
            tmp = float(total_iou_deno_class[l])

            if tmp == 0:
                tmp = 0
            else:
                tmp = total_correct_class[l] / float(total_iou_deno_class[l])


            iou_per_class_str += 'class %s, IoU: %.3f \n' % (
                seg_label_to_cat[l] + ' ' * (14 - len(seg_label_to_cat[l])),tmp )


        log_string(iou_per_class_str)
        log_string('eval point avg class IoU: %f' % np.mean(IoU))
        log_string('eval whole scene point avg class acc: %f' % (
            np.mean(np.array(total_correct_class) / (np.array(total_seen_class, dtype=np.float) + 1e-6))))
        log_string('eval whole scene point accuracy: %f' % (
                np.sum(total_correct_class) / float(np.sum(total_seen_class) + 1e-6)))

        print("Done!")


if __name__ == '__main__':
    args = parse_args()
    main(args)