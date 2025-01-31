import tqdm
import numpy as np
from pathlib import Path
import torchsummary
import math
import torch
import random
from tensorboardX import SummaryWriter
import argparse
import datetime

import models
import losses
import utils
import dataset
import scheduler
import layers

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Self-supervised Depth Estimation on Monocular Endoscopy Dataset -- Train',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--adjacent_range', nargs='+', type=int, required=True,
                        help='interval range for a pair of video frames')
    parser.add_argument('--id_range', nargs='+', type=int, required=True,
                        help='id range for the training and testing dataset')
    parser.add_argument('--input_downsampling', type=float, default=4.0,
                        help='image downsampling rate')
    parser.add_argument('--input_size', nargs='+', type=int, required=True, help='resolution of network input')
    parser.add_argument('--batch_size', type=int, default=8, help='batch size for training and testing')
    parser.add_argument('--num_workers', type=int, default=8, help='number of workers for input data loader')
    parser.add_argument('--num_pre_workers', type=int, default=8,
                        help='number of workers for preprocessing intermediate data')
    parser.add_argument('--dcl_weight', type=float, default=5.0,
                        help='weight for depth consistency loss in the later training stage')
    parser.add_argument('--sfl_weight', type=float, default=20.0, help='weight for sparse flow loss')
    parser.add_argument('--max_lr', type=float, default=1.0e-3, help='upper bound learning rate for cyclic lr')
    parser.add_argument('--min_lr', type=float, default=1.0e-4, help='lower bound learning rate for cyclic lr')
    parser.add_argument('--num_iter', type=int, default=1000, help='number of iterations per epoch')
    parser.add_argument('--inlier_percentage', type=float, default=0.99,
                        help='percentage of inliers of SfM point clouds (for pruning some outliers)')
    parser.add_argument('--validation_interval', type=int, default=1, help='epoch interval for validation')
    parser.add_argument('--zero_division_epsilon', type=float, default=1.0e-8, help='epsilon to prevent zero division')
    parser.add_argument('--display_interval', type=int, default=10, help='iteration interval for image display')
    parser.add_argument('--training_patient_id', nargs='+', type=int, required=True, help='id of the training patient')
    parser.add_argument('--testing_patient_id', nargs='+', type=int, required=True, help='id of the testing patient')
    parser.add_argument('--validation_patient_id', nargs='+', type=int, required=True,
                        help='id of the valiadtion patient')
    parser.add_argument('--load_intermediate_data', action='store_true', help='whether to load intermediate data')
    parser.add_argument('--load_trained_model', action='store_true',
                        help='whether to load trained student model')
    parser.add_argument('--number_epoch', type=int, required=True, help='number of epochs in total')
    parser.add_argument('--visibility_overlap', type=int, default=30, help='overlap of point visibility information')
    parser.add_argument('--training_result_root', type=str, required=True, help='root of the training input and ouput')
    parser.add_argument('--training_data_root', type=str, required=True, help='path to the training data')
    parser.add_argument('--architecture_summary', action='store_true', help='display the network architecture')
    parser.add_argument('--trained_model_path', type=str, default=None,
                        help='path to the trained student model')

    args = parser.parse_args()

    torch.backends.cudnn.deterministic = False
    torch.backends.cudnn.benchmark = True
    torch.manual_seed(7777)
    np.random.seed(7777)
    random.seed(7777)

    # Hyper-parameters
    adjacent_range = args.adjacent_range
    input_downsampling = args.input_downsampling
    height, width = args.input_size
    batch_size = args.batch_size
    num_workers = args.num_workers
    num_pre_workers = args.num_pre_workers
    depth_consistency_weight = args.dcl_weight
    sparse_flow_weight = args.sfl_weight
    max_lr = args.max_lr
    min_lr = args.min_lr
    num_iter = args.num_iter
    inlier_percentage = args.inlier_percentage
    validation_each = args.validation_interval
    depth_scaling_epsilon = args.zero_division_epsilon
    depth_warping_epsilon = args.zero_division_epsilon
    wsl_epsilon = args.zero_division_epsilon
    display_each = args.display_interval
    training_patient_id = args.training_patient_id
    testing_patient_id = args.testing_patient_id
    validation_patient_id = args.validation_patient_id
    load_intermediate_data = args.load_intermediate_data
    load_trained_model = args.load_trained_model
    n_epochs = args.number_epoch
    training_result_root = args.training_result_root
    display_architecture = args.architecture_summary
    trained_model_path = args.trained_model_path
    training_data_root = Path(args.training_data_root)
    id_range = args.id_range
    visibility_overlap = args.visibility_overlap
    currentDT = datetime.datetime.now()

    depth_estimation_model_teacher = []
    failure_sequences = []

    log_root = Path(training_result_root) / "depth_estimation_train_run_{}_{}_{}_{}_test_id_{}".format(
        currentDT.month,
        currentDT.day,
        currentDT.hour,
        currentDT.minute,
        "_".join(str(testing_patient_id)))
    if not log_root.exists():
        log_root.mkdir()
    writer = SummaryWriter(logdir=str(log_root))
    print("Tensorboard visualization at {}".format(str(log_root)))

    # Get color image filenames
    train_filenames, val_filenames, test_filenames = utils.get_color_file_names_by_bag(training_data_root,
                                                                                       training_patient_id=training_patient_id,
                                                                                       validation_patient_id=validation_patient_id,
                                                                                       testing_patient_id=testing_patient_id)
    folder_list = utils.get_parent_folder_names(training_data_root, id_range=id_range)

    # Build training and validation dataset
    train_dataset = dataset.SfMDataset(image_file_names=train_filenames,
                                       folder_list=folder_list,
                                       adjacent_range=adjacent_range, transform=None,
                                       downsampling=input_downsampling,
                                       inlier_percentage=inlier_percentage,
                                       use_store_data=load_intermediate_data,
                                       store_data_root=training_data_root,
                                       phase="train",
                                       num_pre_workers=num_pre_workers, visible_interval=visibility_overlap,
                                       rgb_mode="rgb", num_iter=num_iter)
    validation_dataset = dataset.SfMDataset(image_file_names=val_filenames,
                                            folder_list=folder_list,
                                            adjacent_range=adjacent_range,
                                            transform=None,
                                            downsampling=input_downsampling,
                                            inlier_percentage=inlier_percentage,
                                            use_store_data=True,
                                            store_data_root=training_data_root,
                                            phase="validation",
                                            num_pre_workers=num_pre_workers, visible_interval=visibility_overlap,
                                            rgb_mode="rgb", num_iter=None)

    train_loader = torch.utils.data.DataLoader(dataset=train_dataset, batch_size=batch_size, shuffle=True,
                                               num_workers=num_workers)
    validation_loader = torch.utils.data.DataLoader(dataset=validation_dataset, batch_size=batch_size, shuffle=False,
                                                    num_workers=batch_size)

    depth_estimation_model = models.FCDenseNet().cuda()

    if display_architecture:
        torchsummary.summary(depth_estimation_model, input_size=(3, height, width))
    # Optimizer
    optimizer = torch.optim.Adam(depth_estimation_model.parameters(), lr=max_lr)
    lr_scheduler = scheduler.CyclicLR(optimizer, base_lr=min_lr, max_lr=max_lr, step_size=num_iter)

    # Custom layers
    depth_scaling_layer = layers.DepthScalingLayer(epsilon=depth_scaling_epsilon)
    depth_warping_layer = layers.DepthWarpingLayer(epsilon=depth_warping_epsilon)
    flow_from_depth_layer = layers.FlowfromDepthLayer()
    # Loss functions
    sparse_flow_loss_function = losses.SparseMaskedL1Loss()
    depth_consistency_loss_function = losses.NormalizedDistanceLoss(height=height, width=width)

    # Load previous student model, lr scheduler, and so on
    if load_trained_model:
        if Path(trained_model_path).exists():
            print("Loading {:s} ...".format(trained_model_path))
            state = torch.load(trained_model_path)
            step = state['step']
            epoch = state['epoch']
            depth_estimation_model.load_state_dict(state['model'])
            print('Restored model, epoch {}, step {}'.format(epoch, step))
        else:
            print("No trained model detected")
            raise OSError
    else:
        epoch = 0
        step = 0

    flag = 0

    for epoch in range(epoch, n_epochs + 1):
        # Set the seed correlated to epoch for reproducibility
        torch.manual_seed(777 + epoch)
        np.random.seed(777 + epoch)
        random.seed(777 + epoch)
        depth_estimation_model.train()

        tq = tqdm.tqdm(total=len(train_loader) * batch_size, dynamic_ncols=True, ncols=40)

        if epoch <= 20:
            depth_consistency_weight = 0.1
        else:
            depth_consistency_weight = args.dcl_weight

        for batch, (
                colors_1, colors_2, sparse_depths_1, sparse_depths_2, sparse_depth_masks_1, sparse_depth_masks_2,
                sparse_flows_1, sparse_flows_2, sparse_flow_masks_1, sparse_flow_masks_2, boundaries, rotations_1_wrt_2,
                rotations_2_wrt_1, translations_1_wrt_2, translations_2_wrt_1, intrinsics, folders, file_names) in \
                enumerate(train_loader):

            lr_scheduler.batch_step(batch_iteration=step)
            tq.set_description('Epoch {}, lr {}'.format(epoch, lr_scheduler.get_lr()))

            with torch.no_grad():
                colors_1 = colors_1.cuda()
                colors_2 = colors_2.cuda()
                sparse_depths_1 = sparse_depths_1.cuda()
                sparse_depths_2 = sparse_depths_2.cuda()
                sparse_depth_masks_1 = sparse_depth_masks_1.cuda()
                sparse_depth_masks_2 = sparse_depth_masks_2.cuda()
                sparse_flows_1 = sparse_flows_1.cuda()
                sparse_flows_2 = sparse_flows_2.cuda()
                sparse_flow_masks_1 = sparse_flow_masks_1.cuda()
                sparse_flow_masks_2 = sparse_flow_masks_2.cuda()
                boundaries = boundaries.cuda()
                rotations_1_wrt_2 = rotations_1_wrt_2.cuda()
                rotations_2_wrt_1 = rotations_2_wrt_1.cuda()
                translations_1_wrt_2 = translations_1_wrt_2.cuda()
                translations_2_wrt_1 = translations_2_wrt_1.cuda()
                intrinsics = intrinsics.cuda()

            colors_1 = boundaries * colors_1
            colors_2 = boundaries * colors_2

            predicted_depth_maps_1 = depth_estimation_model(colors_1)
            predicted_depth_maps_2 = depth_estimation_model(colors_2)

            scaled_depth_maps_1, normalized_scale_std_1 = depth_scaling_layer(
                [predicted_depth_maps_1, sparse_depths_1, sparse_depth_masks_1])
            scaled_depth_maps_2, normalized_scale_std_2 = depth_scaling_layer(
                [predicted_depth_maps_2, sparse_depths_2, sparse_depth_masks_2])

            # Sparse flow loss
            flows_from_depth_1 = flow_from_depth_layer(
                [scaled_depth_maps_1, boundaries, translations_1_wrt_2, rotations_1_wrt_2,
                 intrinsics])
            flows_from_depth_2 = flow_from_depth_layer(
                [scaled_depth_maps_2, boundaries, translations_2_wrt_1, rotations_2_wrt_1,
                 intrinsics])
            sparse_flow_masks_1 = sparse_flow_masks_1 * boundaries
            sparse_flow_masks_2 = sparse_flow_masks_2 * boundaries
            sparse_flows_1 = sparse_flows_1 * boundaries
            sparse_flows_2 = sparse_flows_2 * boundaries
            flows_from_depth_1 = flows_from_depth_1 * boundaries
            flows_from_depth_2 = flows_from_depth_2 * boundaries

            sparse_flow_loss = sparse_flow_weight * 0.5 * (sparse_flow_loss_function(
                [sparse_flows_1, flows_from_depth_1, sparse_flow_masks_1]) + sparse_flow_loss_function(
                [sparse_flows_2, flows_from_depth_2, sparse_flow_masks_2]))

            # Depth consistency loss
            warped_depth_maps_2_to_1, intersect_masks_1 = depth_warping_layer(
                [scaled_depth_maps_1, scaled_depth_maps_2, boundaries, translations_1_wrt_2, rotations_1_wrt_2,
                 intrinsics])
            warped_depth_maps_1_to_2, intersect_masks_2 = depth_warping_layer(
                [scaled_depth_maps_2, scaled_depth_maps_1, boundaries, translations_2_wrt_1, rotations_2_wrt_1,
                 intrinsics])
            depth_consistency_loss = depth_consistency_weight * 0.5 * (depth_consistency_loss_function(
                [scaled_depth_maps_1, warped_depth_maps_2_to_1, intersect_masks_1,
                 intrinsics]) + depth_consistency_loss_function(
                [scaled_depth_maps_2, warped_depth_maps_1_to_2, intersect_masks_2, intrinsics]))
            loss = depth_consistency_loss + sparse_flow_loss

            if math.isnan(loss.item()) or math.isinf(loss.item()):
                optimizer.zero_grad()
                loss.backward()
                optimizer.zero_grad()
                optimizer.step()
                continue
            else:
                flag += 1
                optimizer.zero_grad()
                loss.backward()
                # Prevent one sample from having too much impact on the training
                torch.nn.utils.clip_grad_norm_(depth_estimation_model.parameters(), 10.0)
                optimizer.step()
                if batch == 0 or flag == 1:
                    mean_loss = loss.item()
                    mean_depth_consistency_loss = depth_consistency_loss.item()
                    mean_sparse_flow_loss = sparse_flow_loss.item()
                else:
                    mean_loss = (mean_loss * batch + loss.item()) / (batch + 1.0)
                    mean_depth_consistency_loss = (mean_depth_consistency_loss * batch +
                                                   depth_consistency_loss.item()) / (batch + 1.0)
                    mean_sparse_flow_loss = (mean_sparse_flow_loss * batch + sparse_flow_loss.item()) / (batch + 1.0)

            step += 1
            tq.update(batch_size)
            tq.set_postfix(loss='avg: {:.5f} cur: {:.5f}'.format(mean_loss, loss.item()),
                           loss_depth_consistency='avg: {:.5f} cur: {:.5f}'.format(
                               mean_depth_consistency_loss,
                               depth_consistency_loss.item()),
                           loss_sparse_flow='avg: {:.5f} cur: {:.5f}'.format(
                               mean_sparse_flow_loss,
                               sparse_flow_loss.item()))
            writer.add_scalars('Training', {'overall': mean_loss,
                                            'depth_consistency': mean_depth_consistency_loss,
                                            'sparse_flow': mean_sparse_flow_loss}, step)

            # Display depth and color at TensorboardX
            if batch % display_each == 0:
                colors_1_display, sparse_depths_1_display, pred_depths_1_display, warped_depths_1_display, sparse_flows_1_display, dense_flows_1_display = \
                    utils.display_color_sparse_depth_dense_depth_warped_depth_sparse_flow_dense_flow(idx=1, step=step,
                                                                                                     writer=writer,
                                                                                                     colors_1=colors_1,
                                                                                                     sparse_depths_1=sparse_depths_1,
                                                                                                     pred_depths_1=scaled_depth_maps_1 * boundaries,
                                                                                                     warped_depths_2_to_1=warped_depth_maps_2_to_1,
                                                                                                     sparse_flows_1=sparse_flows_1,
                                                                                                     flows_from_depth_1=flows_from_depth_1,
                                                                                                     phase="Training",
                                                                                                     is_return_image=True,
                                                                                                     color_reverse=True,
                                                                                                     rgb_mode="rgb",
                                                                                                     boundaries=boundaries
                                                                                                     )
                colors_2_display, sparse_depths_2_display, pred_depths_2_display, warped_depths_2_display, sparse_flows_2_display, dense_flows_2_display = \
                    utils.display_color_sparse_depth_dense_depth_warped_depth_sparse_flow_dense_flow(idx=2, step=step,
                                                                                                     writer=writer,
                                                                                                     colors_1=colors_2,
                                                                                                     sparse_depths_1=sparse_depths_2,
                                                                                                     pred_depths_1=scaled_depth_maps_2 * boundaries,
                                                                                                     warped_depths_2_to_1=warped_depth_maps_1_to_2,
                                                                                                     sparse_flows_1=sparse_flows_2,
                                                                                                     flows_from_depth_1=flows_from_depth_2,
                                                                                                     phase="Training",
                                                                                                     is_return_image=True,
                                                                                                     color_reverse=True,
                                                                                                     rgb_mode="rgb",
                                                                                                     boundaries=boundaries
                                                                                                     )
                image_display = utils.stack_and_display(phase="Training",
                                                        title="Results (c1, sd1, d1, wd1, sf1, df1, c2, sd2, d2, wd2, sf2, df2)",
                                                        step=step, writer=writer,
                                                        image_list=[colors_1_display, sparse_depths_1_display,
                                                                    pred_depths_1_display,
                                                                    warped_depths_1_display, sparse_flows_1_display,
                                                                    dense_flows_1_display,
                                                                    colors_2_display, sparse_depths_2_display,
                                                                    pred_depths_2_display,
                                                                    warped_depths_2_display, sparse_flows_2_display,
                                                                    dense_flows_2_display],
                                                        return_image=True)
        tq.close()

        # Save student model
        if epoch % validation_each != 0:
            continue

        tq = tqdm.tqdm(total=len(validation_loader) * batch_size, dynamic_ncols=True, ncols=40)
        tq.set_description('Validation Epoch {}'.format(epoch))
        with torch.no_grad():
            for batch, (
                    colors_1, colors_2, sparse_depths_1, sparse_depths_2, sparse_depth_masks_1,
                    sparse_depth_masks_2, sparse_flows_1,
                    sparse_flows_2, sparse_flow_masks_1, sparse_flow_masks_2, boundaries, rotations_1_wrt_2,
                    rotations_2_wrt_1, translations_1_wrt_2, translations_2_wrt_1, intrinsics,
                    folders, file_names) in enumerate(validation_loader):

                colors_1 = colors_1.cuda()
                colors_2 = colors_2.cuda()
                sparse_depths_1 = sparse_depths_1.cuda()
                sparse_depths_2 = sparse_depths_2.cuda()
                sparse_depth_masks_1 = sparse_depth_masks_1.cuda()
                sparse_depth_masks_2 = sparse_depth_masks_2.cuda()
                sparse_flows_1 = sparse_flows_1.cuda()
                sparse_flows_2 = sparse_flows_2.cuda()
                sparse_flow_masks_1 = sparse_flow_masks_1.cuda()
                sparse_flow_masks_2 = sparse_flow_masks_2.cuda()
                boundaries = boundaries.cuda()
                rotations_1_wrt_2 = rotations_1_wrt_2.cuda()
                rotations_2_wrt_1 = rotations_2_wrt_1.cuda()
                translations_1_wrt_2 = translations_1_wrt_2.cuda()
                translations_2_wrt_1 = translations_2_wrt_1.cuda()
                intrinsics = intrinsics.cuda()

                colors_1 = boundaries * colors_1
                colors_2 = boundaries * colors_2

                predicted_depth_maps_1 = depth_estimation_model(colors_1)
                predicted_depth_maps_2 = depth_estimation_model(colors_2)

                scaled_depth_maps_1, normalized_scale_std_1 = depth_scaling_layer(
                    [torch.abs(predicted_depth_maps_1), sparse_depths_1, sparse_depth_masks_1])
                scaled_depth_maps_2, normalized_scale_std_2 = depth_scaling_layer(
                    [torch.abs(predicted_depth_maps_2), sparse_depths_2, sparse_depth_masks_2])

                # Sparse flow loss
                flows_from_depth_1 = flow_from_depth_layer(
                    [scaled_depth_maps_1, boundaries, translations_1_wrt_2, rotations_1_wrt_2,
                     intrinsics])
                flows_from_depth_2 = flow_from_depth_layer(
                    [scaled_depth_maps_2, boundaries, translations_2_wrt_1, rotations_2_wrt_1,
                     intrinsics])
                sparse_flow_masks_1 = sparse_flow_masks_1 * boundaries
                sparse_flow_masks_2 = sparse_flow_masks_2 * boundaries
                sparse_flows_1 = sparse_flows_1 * boundaries
                sparse_flows_2 = sparse_flows_2 * boundaries
                flows_from_depth_1 = flows_from_depth_1 * boundaries
                flows_from_depth_2 = flows_from_depth_2 * boundaries
                sparse_flow_loss = sparse_flow_weight * 0.5 * (sparse_flow_loss_function(
                    [sparse_flows_1, flows_from_depth_1, sparse_flow_masks_1]) + sparse_flow_loss_function(
                    [sparse_flows_2, flows_from_depth_2, sparse_flow_masks_2]))

                # Depth consistency loss
                warped_depth_maps_2_to_1, intersect_masks_1 = depth_warping_layer(
                    [scaled_depth_maps_1, scaled_depth_maps_2, boundaries, translations_1_wrt_2, rotations_1_wrt_2,
                     intrinsics])
                warped_depth_maps_1_to_2, intersect_masks_2 = depth_warping_layer(
                    [scaled_depth_maps_2, scaled_depth_maps_1, boundaries, translations_2_wrt_1, rotations_2_wrt_1,
                     intrinsics])
                depth_consistency_loss = depth_consistency_weight * 0.5 * (depth_consistency_loss_function(
                    [scaled_depth_maps_1, warped_depth_maps_2_to_1,
                     intersect_masks_1, intrinsics]) + depth_consistency_loss_function(
                    [scaled_depth_maps_2, warped_depth_maps_1_to_2, intersect_masks_2, intrinsics]))

                loss = depth_consistency_loss + sparse_flow_loss
                tq.update(batch_size)
                if not np.isnan(loss.item()):
                    if batch == 0:
                        mean_loss = loss.item()
                        mean_depth_consistency_loss = depth_consistency_loss.item()
                        mean_sparse_flow_loss = sparse_flow_loss.item()
                    else:
                        mean_loss = (mean_loss * batch + loss.item()) / (batch + 1.0)
                        mean_depth_consistency_loss = (mean_depth_consistency_loss * batch +
                                                       depth_consistency_loss.item()) / (batch + 1.0)
                        mean_sparse_flow_loss = (mean_sparse_flow_loss * batch + sparse_flow_loss.item()) / (
                                batch + 1.0)

                # Display depth and color at TensorboardX
                if batch % display_each == 0:
                    colors_1_display, pred_depths_1_display, sparse_flows_1_display, dense_flows_1_display = \
                        utils.display_color_depth_sparse_flow_dense_flow(1, step, writer, colors_1,
                                                                         scaled_depth_maps_1 * boundaries,
                                                                         sparse_flows_1, flows_from_depth_1,
                                                                         phase="Validation", is_return_image=True,
                                                                         color_reverse=True)

                    colors_2_display, pred_depths_2_display, sparse_flows_2_display, dense_flows_2_display = \
                        utils.display_color_depth_sparse_flow_dense_flow(2, step, writer, colors_2,
                                                                         scaled_depth_maps_2 * boundaries,
                                                                         sparse_flows_2, flows_from_depth_2,
                                                                         phase="Validation", is_return_image=True,
                                                                         color_reverse=True)
                    utils.stack_and_display(phase="Validation", title="Results (c1, d1, sf1, df1, c2, d2, sf2, df2)",
                                            step=step, writer=writer,
                                            image_list=[colors_1_display, pred_depths_1_display, sparse_flows_1_display,
                                                        dense_flows_1_display,
                                                        colors_2_display, pred_depths_2_display, sparse_flows_2_display,
                                                        dense_flows_2_display])

                # TensorboardX
                writer.add_scalars('Validation', {'overall': mean_loss,
                                                  'depth_consistency': mean_depth_consistency_loss,
                                                  'sparse_flow': mean_sparse_flow_loss}, epoch)

        tq.close()
        model_path_epoch = log_root / 'checkpoint_model_epoch_{}_validation_{}.pt'.format(epoch,
                                                                                          mean_sparse_flow_loss)
        utils.save_model(model=depth_estimation_model, optimizer=optimizer,
                         epoch=epoch + 1, step=step, model_path=model_path_epoch,
                         validation_loss=mean_sparse_flow_loss)

    writer.close()
