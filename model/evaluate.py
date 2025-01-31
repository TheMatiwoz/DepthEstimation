import tqdm
import cv2
import numpy as np
from pathlib import Path
import torch
import random
from tensorboardX import SummaryWriter
import argparse
import datetime
# Local
import models
import utils
import dataset

if __name__ == '__main__':

    parser = argparse.ArgumentParser(
        description='Self-supervised Depth Estimation on Monocular Endoscopy Dataset -- Evaluate',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--input_downsampling', type=float, default=4.0,
                        help='image downsampling rate')
    parser.add_argument('--input_size', nargs='+', type=int, required=True,
                        help='input size')
    parser.add_argument('--selected_frame_index_list', nargs='+', type=int, required=False, default=None,
                        help='selected frame index list)')
    parser.add_argument('--batch_size', type=int, default=1, help='batch size for testing')
    parser.add_argument('--num_workers', type=int, default=2, help='number of workers for input data loader')
    parser.add_argument('--num_pre_workers', type=int, default=8,
                        help='number of workers for preprocessing intermediate data')
    parser.add_argument('--adjacent_range', nargs='+', type=int, required=True,
                        help='interval range for a pair of video frames')
    parser.add_argument('--id_range', nargs='+', type=int, required=True,
                        help='id range for the training and testing dataset')
    parser.add_argument('--inlier_percentage', type=float, default=0.995,
                        help='percentage of inliers of SfM point clouds (for pruning some outliers)')
    parser.add_argument('--testing_patient_id', nargs='+', type=int, help='id of the testing patient')
    parser.add_argument('--load_intermediate_data', action='store_true', help='whether to load intermediate data')
    parser.add_argument('--use_hsv_colorspace', action='store_true',
                        help='convert RGB to hsv colorspace')
    parser.add_argument('--architecture_summary', action='store_true', help='display the network architecture')
    parser.add_argument('--load_all_frames', action='store_true',
                        help='whether or not to load all frames in sequence root')
    parser.add_argument('--trained_model_path', type=str, required=True, help='path to the trained student model')
    parser.add_argument('--sequence_root', type=str, required=True, help='path to the testing sequence')
    parser.add_argument('--evaluation_result_root', type=str, required=True,
                        help='logging root')
    parser.add_argument('--evaluation_data_root', type=str, required=True, help='path to the testing data')
    parser.add_argument('--visibility_overlap', type=int, default=30, help='overlap of point visibility information')
    args = parser.parse_args()

    # Fix randomness for reproducibility
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    torch.manual_seed(10085)
    np.random.seed(10085)
    random.seed(10085)

    # Hyper-parameters
    height, width = args.input_size
    adjacent_range = args.adjacent_range
    id_range = args.id_range
    input_downsampling = args.input_downsampling
    batch_size = args.batch_size
    num_workers = args.num_workers
    inlier_percentage = args.inlier_percentage
    testing_patient_id = args.testing_patient_id
    load_intermediate_data = args.load_intermediate_data
    display_architecture = args.architecture_summary
    selected_frame_index_list = args.selected_frame_index_list
    load_all_frames = args.load_all_frames
    evaluation_result_root = Path(args.evaluation_result_root)
    evaluation_data_root = Path(args.evaluation_data_root)
    trained_model_path = Path(args.trained_model_path)
    sequence_root = Path(args.sequence_root)
    visibility_overlap = args.visibility_overlap
    num_pre_workers = args.num_pre_workers
    currentDT = datetime.datetime.now()

    log_root = Path(evaluation_result_root) / "depth_estimation_evaluation_run_{}_{}_{}_{}_test_id_{}".format(
        currentDT.month,
        currentDT.day,
        currentDT.hour,
        currentDT.minute,
        "_".join(str(testing_patient_id)))
    if not log_root.exists():
        log_root.mkdir(parents=True)
    writer = SummaryWriter(logdir=str(log_root))
    print("Tensorboard visualization at {}".format(str(log_root)))

    # Read all frame indexes
    selected_frame_index_list = utils.read_visible_view_indexes(sequence_root)

    # Get color image filenames
    test_filenames = utils.get_filenames_from_frame_indexes(sequence_root, selected_frame_index_list)
    folder_list = utils.get_parent_folder_names(evaluation_data_root, id_range=id_range)

    test_dataset = dataset.SfMDataset(image_file_names=test_filenames,
                                      folder_list=folder_list,
                                      adjacent_range=adjacent_range, transform=None,
                                      downsampling=input_downsampling,
                                      inlier_percentage=inlier_percentage,
                                      use_store_data=load_intermediate_data,
                                      store_data_root=evaluation_data_root,
                                      phase="test",
                                      num_pre_workers=num_pre_workers, visible_interval=visibility_overlap,
                                      rgb_mode="rgb")

    test_loader = torch.utils.data.DataLoader(dataset=test_dataset, batch_size=1, shuffle=False,
                                              num_workers=0)
    depth_estimation_model = models.FCDenseNet().cuda()

    # Load trained model
    if trained_model_path.exists():
        print("Loading {:s} ...".format(str(trained_model_path)))
        state = torch.load(str(trained_model_path))
        step = state['step']
        epoch = state['epoch']
        depth_estimation_model.load_state_dict(state['model'])
        print('Restored model, epoch {}, step {}'.format(epoch, step))
    else:
        print("Trained model does not exist")
        raise OSError

    with torch.no_grad():
        depth_estimation_model.eval()
        tq = tqdm.tqdm(total=len(test_loader) * batch_size)
        for batch, (colors_1, boundaries, intrinsics, names) in enumerate(test_loader):
            colors_1 = colors_1.cuda()
            boundaries = boundaries.cuda()

            colors_1 = boundaries * colors_1
            predicted_depth_maps_1 = depth_estimation_model(colors_1)

            color_display = np.uint8(
                255 * (0.5 * colors_1[0].permute(1, 2, 0).data.cpu().numpy() + 0.5).reshape((height, width, 3)))

            color_display = cv2.cvtColor(color_display, cv2.COLOR_RGB2BGR)

            boundary = boundaries[0].data.cpu().numpy().reshape((height, width))
            color_display = np.uint8(boundary.reshape((height, width, 1)) * color_display)
            depth_map = (boundaries * predicted_depth_maps_1)[0].data.cpu().numpy().reshape((height, width))
            depth_display = cv2.applyColorMap(np.uint8(255 * depth_map / np.max(depth_map)), cv2.COLORMAP_JET)

            cv2.imwrite(str(log_root / "{}.png".format(names[0])), cv2.hconcat([color_display, depth_display]))
            tq.update(batch_size)
