# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Training script for permute MNIST experiment.
"""
from __future__ import print_function

import argparse
import os
import sys
import math
import time

import datetime
import numpy as np
import tensorflow as tf
from copy import deepcopy
from six.moves import cPickle as pickle

from utils.data_utils import construct_permute_mnist
from utils.er_utils import update_reservior, update_fifo_buffer, er_mem_update_hindsight, update_avg_image_vectors
from utils.utils import get_sample_weights, sample_from_dataset, update_episodic_memory, concatenate_datasets, samples_for_each_class, sample_from_dataset_icarl, average_acc_stats_across_runs, average_fgt_stats_across_runs
from utils.vis_utils import plot_acc_multiple_runs, plot_histogram, snapshot_experiment_meta_data, snapshot_experiment_eval
from model import Model

###############################################################
################ Some definitions #############################
### These will be edited by the command line options ##########
###############################################################

## Training Options
NUM_RUNS = 10           # Number of experiments to average over
TRAIN_ITERS = 5000      # Number of training iterations per task
BATCH_SIZE = 16
LEARNING_RATE = 1e-3    
RANDOM_SEED = 1234
VALID_OPTIMS = ['SGD', 'MOMENTUM', 'ADAM']
OPTIM = 'SGD'
OPT_POWER = 0.9
OPT_MOMENTUM = 0.9
VALID_ARCHS = ['FC-S', 'FC-B']
ARCH = 'FC-S'

## Model options
MODELS = ['VAN', 'PI', 'EWC', 'MAS', 'RWALK', 'A-GEM', 'S-GEM', 'FTR_EXT', 'MER', 'ER-Reservoir', 'ER-Ring', 'ER-Hindsight-Anchors'] #List of valid models 
IMP_METHOD = 'EWC'
SYNAP_STGTH = 75000
FISHER_EMA_DECAY = 0.9      # Exponential moving average decay factor for Fisher computation (online Fisher)
FISHER_UPDATE_AFTER = 10    # Number of training iterations for which the F_{\theta}^t is computed (see Eq. 10 in RWalk paper) 
SAMPLES_PER_CLASS = 25   # Number of samples per task
INPUT_FEATURE_SIZE = 784
IMG_HEIGHT = 28
IMG_WIDTH = 28
IMG_CHANNELS = 1
TOTAL_CLASSES = 10          # Total number of classes in the dataset 
EPS_MEM_BATCH_SIZE = 256
DEBUG_EPISODIC_MEMORY = False
USE_GPU = True
K_FOR_CROSS_VAL = 3
TIME_MY_METHOD = False
COUNT_VIOLATIONS = False
MEASURE_PERF_ON_EPS_MEMORY = False
# MER Specific options
MER_S = 10
MER_BETA = 0.1
MER_GAMMA = 0.1

## Logging, saving and testing options
LOG_DIR = './permute_mnist_results'

## Evaluation options

## Num Tasks
NUM_TASKS = 23
MULTI_TASK = False

def normalize_tensors(a):
    num_tensors = a.shape[0]
    for i in range(num_tensors):
        a[i] = (a[i] - a[i].min())/ (a[i].max() - a[i].min())
    return a


def get_arguments():
    """Parse all the arguments provided from the CLI.

    Returns:
      A list of parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Script for permutted mnist experiment.")
    parser.add_argument("--cross-validate-mode", action="store_true",
                       help="If option is chosen then snapshoting after each batch is disabled")
    parser.add_argument("--online-cross-val", action="store_true",
                       help="If option is chosen then enable the online cross validation of the learning rate")
    parser.add_argument("--train-single-epoch", action="store_true",
                       help="If option is chosen then train for single epoch")
    parser.add_argument("--eval-single-head", action="store_true",
                       help="If option is chosen then evaluate on a single head setting.")
    parser.add_argument("--arch", type=str, default=ARCH, help="Network Architecture for the experiment.\
                        \n \nSupported values: %s"%(VALID_ARCHS))
    parser.add_argument("--num-runs", type=int, default=NUM_RUNS,
                       help="Total runs/ experiments over which accuracy is averaged.")
    parser.add_argument("--train-iters", type=int, default=TRAIN_ITERS,
                       help="Number of training iterations for each task.")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                       help="Mini-batch size for each task.")
    parser.add_argument("--random-seed", type=int, default=RANDOM_SEED,
                        help="Random Seed.")
    parser.add_argument("--learning-rate", type=float, default=LEARNING_RATE,
                       help="Starting Learning rate for each task.")
    parser.add_argument("--optim", type=str, default=OPTIM,
                        help="Optimizer for the experiment. \
                                \n \nSupported values: %s"%(VALID_OPTIMS))
    parser.add_argument("--imp-method", type=str, default=IMP_METHOD,
                       help="Model to be used for LLL. \
                        \n \nSupported values: %s"%(MODELS))
    parser.add_argument("--synap-stgth", type=float, default=SYNAP_STGTH,
                       help="Synaptic strength for the regularization.")
    parser.add_argument("--fisher-ema-decay", type=float, default=FISHER_EMA_DECAY,
                       help="Exponential moving average decay for Fisher calculation at each step.")
    parser.add_argument("--fisher-update-after", type=int, default=FISHER_UPDATE_AFTER,
                       help="Number of training iterations after which the Fisher will be updated.")
    parser.add_argument("--mem-size", type=int, default=SAMPLES_PER_CLASS,
                       help="Number of samples per class from previous tasks.")
    parser.add_argument("--eps-mem-batch", type=int, default=EPS_MEM_BATCH_SIZE,
                       help="Number of samples per class from previous tasks.")
    parser.add_argument("--examples-per-task", type=int, default=1000,
                       help="Number of examples per task.")
    parser.add_argument("--anchor-eta", type=float, default=0.1,
                        help="Neural mean embedding strength for anchor generation.")
    parser.add_argument("--log-dir", type=str, default=LOG_DIR,
                       help="Directory where the plots and model accuracies will be stored.")
    return parser.parse_args()

def train_task_sequence(model, sess, args):
    """
    Train and evaluate LLL system such that we only see a example once
    Args:
    Returns:
        dict    A dictionary containing mean and stds for the experiment
    """
    # List to store accuracy for each run
    runs = []

    batch_size = args.batch_size

    if model.imp_method == 'A-GEM' or model.imp_method == 'MER' or 'ER-' in model.imp_method:
        use_episodic_memory = True
    else:
        use_episodic_memory = False

    # Loop over number of runs to average over
    for runid in range(args.num_runs):
        print('\t\tRun %d:'%(runid))

        # Initialize the random seeds
        np.random.seed(args.random_seed+runid)

        time_start = time.time()
        # Load the permute mnist dataset
        datasets = construct_permute_mnist(model.num_tasks)
        time_end = time.time()
        time_spent = time_end - time_start
        print('Data loading time: {}'.format(time_spent))

        episodic_mem_size = args.mem_size*model.num_tasks*TOTAL_CLASSES

        # Initialize all the variables in the model
        sess.run(tf.global_variables_initializer())

        # Run the init ops
        model.init_updates(sess)

        # List to store accuracies for a run
        evals = []

        # List to store the classes that we have so far - used at test time
        test_labels = np.arange(TOTAL_CLASSES)

        if use_episodic_memory:
            # Reserve a space for episodic memory
            episodic_images = np.zeros([episodic_mem_size, INPUT_FEATURE_SIZE])
            episodic_labels = np.zeros([episodic_mem_size, TOTAL_CLASSES])
            count_cls = np.zeros(TOTAL_CLASSES, dtype=np.int32)
            episodic_filled_counter = 0
            examples_seen_so_far = 0
            if model.imp_method == 'ER-Hindsight-Anchors':
                avg_img_vectors = np.zeros([TOTAL_CLASSES, INPUT_FEATURE_SIZE])
                anchor_images = np.zeros([model.num_tasks*TOTAL_CLASSES, INPUT_FEATURE_SIZE])
                anchor_labels = np.zeros([model.num_tasks*TOTAL_CLASSES, TOTAL_CLASSES])
                anchor_count_cls = np.zeros(TOTAL_CLASSES, dtype=np.int32)

        # Mask for softmax
        # Since all the classes are present in all the tasks so nothing to mask
        logit_mask = np.ones(TOTAL_CLASSES)

        if COUNT_VIOLATIONS:
            violation_count = np.zeros(model.num_tasks)
            vc = 0

        # Training loop for all the tasks
        for task in range(len(datasets)):
            print('\t\tTask %d:'%(task))

            anchors_counter = task*TOTAL_CLASSES  # 1 per class per task

            # If not the first task then restore weights from previous task
            if(task > 0):
                model.restore(sess)

            if MULTI_TASK:
                if task == 0:
                    # Extract training images and labels for the current task
                    task_train_images = datasets[task]['train']['images']
                    task_train_labels = datasets[task]['train']['labels']
                    sample_weights = np.ones([task_train_labels.shape[0]], dtype=np.float32)
                    total_train_examples = task_train_images.shape[0]
                    # Randomly suffle the training examples
                    perm = np.arange(total_train_examples)
                    np.random.shuffle(perm)
                    train_x = task_train_images[perm][:args.examples_per_task]
                    train_y = task_train_labels[perm][:args.examples_per_task]
                    task_sample_weights = sample_weights[perm][:args.examples_per_task]
                    for t_ in range(1, len(datasets)):
                        task_train_images = datasets[t_]['train']['images']
                        task_train_labels = datasets[t_]['train']['labels']
                        sample_weights = np.ones([task_train_labels.shape[0]], dtype=np.float32)
                        total_train_examples = task_train_images.shape[0]
                        # Randomly suffle the training examples
                        perm = np.arange(total_train_examples)
                        np.random.shuffle(perm)
                        train_x = np.concatenate((train_x, task_train_images[perm][:args.examples_per_task]), axis=0)
                        train_y = np.concatenate((train_y, task_train_labels[perm][:args.examples_per_task]), axis=0)
                        task_sample_weights = np.concatenate((task_sample_weights, sample_weights[perm][:args.examples_per_task]), axis=0)

                    perm = np.arange(train_x.shape[0])
                    np.random.shuffle(perm)
                    train_x = train_x[perm]
                    train_y = train_y[perm]
                    task_sample_weights = task_sample_weights[perm]
                else:
                    # Skip training for this task
                    continue

            else:
                # Extract training images and labels for the current task
                task_train_images = datasets[task]['train']['images']
                task_train_labels = datasets[task]['train']['labels']
                # Assign equal weights to all the examples
                task_sample_weights = np.ones([task_train_labels.shape[0]], dtype=np.float32)
                total_train_examples = task_train_images.shape[0]
                # Randomly suffle the training examples
                perm = np.arange(total_train_examples)
                np.random.shuffle(perm)
                train_x = task_train_images[perm][:args.examples_per_task]
                train_y = task_train_labels[perm][:args.examples_per_task]
                task_sample_weights = task_sample_weights[perm][:args.examples_per_task]
 
            print('Received {} images, {} labels at task {}'.format(train_x.shape[0], train_y.shape[0], task))

            # Array to store accuracies when training for task T
            ftask = []
            
            num_train_examples = train_x.shape[0]

            # Train a task observing sequence of data
            if args.train_single_epoch:
                num_iters = (num_train_examples + batch_size - 1) // batch_size
            else:
                num_iters = args.train_iters

            # Training loop for task T
            for iters in range(num_iters):

                if args.train_single_epoch and not args.cross_validate_mode:
                    if (iters < 10) or (iters < 100 and iters % 10 == 0) or (iters % 100 == 0):
                        # Snapshot the current performance across all tasks after each mini-batch
                        fbatch = test_task_sequence(model, sess, datasets, args.online_cross_val)
                        ftask.append(fbatch)

                offset = (iters * batch_size) % num_train_examples
                if (offset+batch_size <= num_train_examples):
                    residual = batch_size
                else:
                    residual = num_train_examples - offset

                feed_dict = {model.x: train_x[offset:offset+batch_size], model.y_: train_y[offset:offset+batch_size], 
                            model.sample_weights: task_sample_weights[offset:offset+batch_size],
                            model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0, 
                            model.output_mask: logit_mask, model.learning_rate: args.learning_rate}

                if model.imp_method == 'VAN':
                    _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'FTR_EXT':
                    if task == 0:
                        _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)
                    else:
                        _, loss = sess.run([model.train_classifier, model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'EWC':
                    # If first iteration of the first task then set the initial value of the running fisher
                    if task == 0 and iters == 0:
                        sess.run([model.set_initial_running_fisher], feed_dict=feed_dict)
                    # Update fisher after every few iterations
                    if (iters + 1) % model.fisher_update_after == 0:
                        sess.run(model.set_running_fisher)
                        sess.run(model.reset_tmp_fisher)
                    
                    _, _, loss = sess.run([model.set_tmp_fisher, model.train, model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'PI':
                    _, _, _, loss = sess.run([model.weights_old_ops_grouped, model.train, model.update_small_omega, 
                                              model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'MAS':
                    _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'A-GEM':
                    if task == 0:
                        # Normal application of gradients
                        _, loss = sess.run([model.train_first_task, model.agem_loss], feed_dict=feed_dict)
                    else:
                        ## Compute and store the reference gradients on the previous tasks
                        if episodic_filled_counter <= args.eps_mem_batch:
                            mem_sample_mask = np.arange(episodic_filled_counter)
                        else:
                            # Sample a random subset from episodic memory buffer
                            mem_sample_mask = np.random.choice(episodic_filled_counter, args.eps_mem_batch, replace=False) # Sample without replacement so that we don't sample an example more than once
                        # Store the reference gradient
                        sess.run(model.store_ref_grads, feed_dict={model.x: episodic_images[mem_sample_mask], model.y_: episodic_labels[mem_sample_mask],
                                                                   model.keep_prob: 1.0, model.output_mask: logit_mask, model.learning_rate: args.learning_rate})
                        if COUNT_VIOLATIONS:
                            vc, _, loss = sess.run([model.violation_count, model.train_subseq_tasks, model.agem_loss], feed_dict=feed_dict)
                        else:
                            # Compute the gradient for current task and project if need be
                            _, loss = sess.run([model.train_subseq_tasks, model.agem_loss], feed_dict=feed_dict)
                    
                    # Put the batch in the FIFO ring buffer
                    update_fifo_buffer(train_x[offset:offset+residual], train_y[offset:offset+residual], episodic_images, episodic_labels,
                                       np.arange(TOTAL_CLASSES), args.mem_size, count_cls, episodic_filled_counter)

                elif model.imp_method == 'RWALK':
                    # If first iteration of the first task then set the initial value of the running fisher
                    if task == 0 and iters == 0:
                        sess.run([model.set_initial_running_fisher], feed_dict=feed_dict)
                        # Store the current value of the weights
                        sess.run(model.weights_delta_old_grouped)
                    # Update fisher and importance score after every few iterations
                    if (iters + 1) % model.fisher_update_after == 0:
                        # Update the importance score using distance in riemannian manifold   
                        sess.run(model.update_big_omega_riemann)
                        # Now that the score is updated, compute the new value for running Fisher
                        sess.run(model.set_running_fisher)
                        # Store the current value of the weights
                        sess.run(model.weights_delta_old_grouped)
                        # Reset the delta_L
                        sess.run([model.reset_small_omega])

                    _, _, _, _, loss = sess.run([model.set_tmp_fisher, model.weights_old_ops_grouped, 
                        model.train, model.update_small_omega, model.reg_loss], feed_dict=feed_dict)

                elif model.imp_method == 'ER-Reservoir':
                    mem_filled_so_far = examples_seen_so_far if (examples_seen_so_far <= episodic_mem_size) else episodic_mem_size
                    er_mem_indices = np.arange(mem_filled_so_far) if (mem_filled_so_far <= args.eps_mem_batch) else np.random.choice(mem_filled_so_far, args.eps_mem_batch, replace=False)
                    np.random.shuffle(er_mem_indices)
                    er_train_x_batch = np.concatenate((episodic_images[er_mem_indices], train_x[offset:offset+residual]), axis=0)
                    er_train_y_batch = np.concatenate((episodic_labels[er_mem_indices], train_y[offset:offset+residual]), axis=0)
                    feed_dict = {model.x: er_train_x_batch, model.y_: er_train_y_batch,
                                 model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0,
                                 model.output_mask: logit_mask, model.learning_rate: args.learning_rate}
                    _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)

                    # Reservoir update
                    examples_seen_so_far = update_reservior(train_x[offset:offset+residual], train_y[offset:offset+residual], 
                                                            episodic_images, episodic_labels, episodic_mem_size, examples_seen_so_far)

                elif model.imp_method == 'MER':
                    mem_filled_so_far = examples_seen_so_far if (examples_seen_so_far <= episodic_mem_size) else episodic_mem_size
                    mer_mem_indices = np.arange(mem_filled_so_far) if (mem_filled_so_far <= args.eps_mem_batch) else np.random.choice(mem_filled_so_far, args.eps_mem_batch, replace=False)
                    np.random.shuffle(mer_mem_indices)
                    mer_train_x_batch = episodic_images[mer_mem_indices]
                    mer_train_y_batch = episodic_labels[mer_mem_indices]
                    sess.run(model.store_theta_i_not_w)
                    for mer_x, mer_y in zip(mer_train_x_batch, mer_train_y_batch):
                        feed_dict = {model.x: np.expand_dims(mer_x, axis=0), model.y_: np.expand_dims(mer_y, axis=0),
                            model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0,
                            model.output_mask: logit_mask, model.learning_rate: args.learning_rate}
                        _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)
                    feed_dict = {model.x: train_x[offset:offset+residual], model.y_: train_y[offset:offset+residual],
                        model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0,
                        model.output_mask: logit_mask, model.learning_rate: MER_S*args.learning_rate}
                    _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)
                    sess.run(model.with_in_batch_reptile_update, feed_dict={model.mer_beta: MER_BETA}) # In the paper this is 'mer_gamma'
                    # Reservoir update
                    examples_seen_so_far = update_reservior(train_x[offset:offset+residual], train_y[offset:offset+residual], 
                                                            episodic_images, episodic_labels, episodic_mem_size, examples_seen_so_far)

                elif model.imp_method == 'ER-Ring':
                    mem_filled_so_far = episodic_filled_counter if (episodic_filled_counter <= episodic_mem_size) else episodic_mem_size
                    er_mem_indices = np.arange(mem_filled_so_far) if (mem_filled_so_far <= args.eps_mem_batch) else np.random.choice(mem_filled_so_far, args.eps_mem_batch, replace=False)
                    er_train_x_batch = np.concatenate((episodic_images[er_mem_indices], train_x[offset:offset+residual]), axis=0)
                    er_train_y_batch = np.concatenate((episodic_labels[er_mem_indices], train_y[offset:offset+residual]), axis=0)
                    feed_dict = {model.x: er_train_x_batch, model.y_: er_train_y_batch,
                                 model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0,
                                 model.output_mask: logit_mask, model.learning_rate: args.learning_rate}
                    _, loss = sess.run([model.train, model.reg_loss], feed_dict=feed_dict)

                    # Put the batch in the FIFO ring buffer
                    update_fifo_buffer(train_x[offset:offset+residual], train_y[offset:offset+residual], episodic_images, episodic_labels,
                                       np.arange(TOTAL_CLASSES), args.mem_size, count_cls, episodic_filled_counter)

                elif model.imp_method == 'ER-Hindsight-Anchors':
                    anchor_mem_indices = np.arange(anchors_counter) if (anchors_counter <= args.eps_mem_batch) else np.random.choice(anchors_counter,
                                                                                                                                                              args.eps_mem_batch, replace=False)
                    mem_filled_so_far = episodic_filled_counter if (episodic_filled_counter <= episodic_mem_size) else episodic_mem_size
                    er_mem_indices = np.arange(mem_filled_so_far) if (mem_filled_so_far <= args.eps_mem_batch) else np.random.choice(mem_filled_so_far, args.eps_mem_batch, replace=False)
                    er_train_x_batch = np.concatenate((episodic_images[er_mem_indices], train_x[offset:offset+residual]), axis=0)
                    er_train_y_batch = np.concatenate((episodic_labels[er_mem_indices], train_y[offset:offset+residual]), axis=0)
                    feed_dict = {model.x: er_train_x_batch, model.y_: er_train_y_batch,
                                 model.training_iters: num_iters, model.train_step: iters, model.keep_prob: 1.0,
                                 model.output_mask: logit_mask, model.learning_rate: args.learning_rate}
                    feed_dict[model.phi_hat_alpha] = 0.5
                    if task == 0:
                        # Just train on the current task dataset
                        phi_hat, _, loss = sess.run([model.phi_hat, model.train, model.reg_loss], feed_dict=feed_dict)
                        anchor_loss = 0.0
                        task_loss = 0.0
                        # Update the FIFO buffer
                        update_fifo_buffer(train_x[offset:offset+residual], train_y[offset:offset+residual], anchor_images, anchor_labels,
                                    np.arange(TOTAL_CLASSES), 1, anchor_count_cls, anchors_counter)
                    else:
                        # Train on the anchoring loss
                        feed_dict[model.anchor_points] = anchor_images[anchor_mem_indices]
                        phi_hat, _, anchor_loss, task_loss, loss = sess.run([model.phi_hat, model.train_anchor, model.anchor_loss, model.task_loss, model.final_anchor_loss], feed_dict=feed_dict)

                    # Update the average image vectors
                    update_avg_image_vectors(train_x[offset:offset+residual], train_y[offset:offset+residual], avg_img_vectors, running_alpha=0.9)

                    # Put the batch in the FIFO ring buffer
                    update_fifo_buffer(train_x[offset:offset+residual], train_y[offset:offset+residual], episodic_images,
                                    episodic_labels, np.arange(TOTAL_CLASSES), args.mem_size, count_cls, episodic_filled_counter)


                if (iters % 10 == 0):
                    print('Step {:d} {:.3f}'.format(iters, loss))

                if (math.isnan(loss)):
                    print('ERROR: NaNs NaNs Nans!!!')
                    sys.exit(0)

            print('\t\t\t\tTraining for Task%d done!'%(task))

            if model.imp_method == 'A-GEM' and COUNT_VIOLATIONS:
                violation_count[task] = vc
                print('Task {}: Violation Count: {}'.format(task, violation_count))
                sess.run(model.reset_violation_count, feed_dict=feed_dict)
            
            elif model.imp_method == 'ER-Hindsight-Anchors':
                if task == 0:
                    # Anchors are already populated
                    pass
                else:
                    anchor_x = np.zeros([TOTAL_CLASSES, INPUT_FEATURE_SIZE])
                    anchor_y = np.zeros([TOTAL_CLASSES, TOTAL_CLASSES])
                    task_labels = np.arange(TOTAL_CLASSES)
                    anchor_x, anchor_y = er_mem_update_hindsight(model, sess, anchor_x, anchor_y, episodic_images,
                                                                       episodic_labels, episodic_filled_counter, task_labels, logit_mask, phi_hat, avg_img_vectors, args)

                    anchor_images[anchors_counter:anchors_counter+TOTAL_CLASSES] = normalize_tensors(anchor_x)
                    anchor_labels[anchors_counter:anchors_counter+TOTAL_CLASSES] = anchor_y

                # Reset the average image vectors
                avg_img_vectors[:] = 0.0

            # Upaate the episodic memory filled counter
            if use_episodic_memory:
                episodic_filled_counter += args.mem_size * TOTAL_CLASSES
                print('Unique labels in the episodic memory: {}'.format(np.unique(np.nonzero(episodic_labels)[1])))
                print('Labels in the episodic memory: {}'.format(np.nonzero(episodic_labels)[1]))

            # Compute the inter-task updates, Fisher/ importance scores etc
            # Don't calculate the task updates for the last task
            if (task < (len(datasets) - 1)) or MEASURE_PERF_ON_EPS_MEMORY:
                model.task_updates(sess, task, task_train_images, np.arange(TOTAL_CLASSES))
                print('\t\t\t\tTask updates after Task%d done!'%(task))

            if args.train_single_epoch and not args.cross_validate_mode: 
                fbatch = test_task_sequence(model, sess, datasets, False)
                ftask.append(fbatch)
                ftask = np.array(ftask)
            else:
                if MEASURE_PERF_ON_EPS_MEMORY:
                    eps_mem = {
                            'images': episodic_images, 
                            'labels': episodic_labels,
                            }
                    # Measure perf on episodic memory
                    ftask = test_task_sequence(model, sess, eps_mem, args.online_cross_val)
                else:
                    # List to store accuracy for all the tasks for the current trained model
                    ftask = test_task_sequence(model, sess, datasets, args.online_cross_val)
                    print('Task: {}, Acc: {}'.format(task, ftask))
            
            # Store the accuracies computed at task T in a list
            evals.append(ftask)

            # Reset the optimizer
            model.reset_optimizer(sess)

            #-> End for loop task

        runs.append(np.array(evals))
        # End for loop runid

    runs = np.array(runs)

    return runs

def test_task_sequence(model, sess, test_data, cross_validate_mode):
    """
    Snapshot the current performance
    """
    if TIME_MY_METHOD:
        # Only compute the training time
        return np.zeros(model.num_tasks)

    list_acc = []
    logit_mask = np.ones(TOTAL_CLASSES)

    if MEASURE_PERF_ON_EPS_MEMORY:
        for task in range(model.num_tasks):
            mem_offset = task*SAMPLES_PER_CLASS*TOTAL_CLASSES
            feed_dict = {model.x: test_data['images'][mem_offset:mem_offset+SAMPLES_PER_CLASS*TOTAL_CLASSES], 
                    model.y_: test_data['labels'][mem_offset:mem_offset+SAMPLES_PER_CLASS*TOTAL_CLASSES], model.keep_prob: 1.0, 
                    model.output_mask: logit_mask}
            acc = model.accuracy.eval(feed_dict = feed_dict)
            list_acc.append(acc)
        print(list_acc)
        return list_acc

    for task, _ in enumerate(test_data):
        feed_dict = {model.x: test_data[task]['test']['images'], 
                model.y_: test_data[task]['test']['labels'], model.keep_prob: 1.0, 
                model.output_mask: logit_mask}
        acc = model.accuracy.eval(feed_dict = feed_dict)

        list_acc.append(acc)

    return list_acc

def main():
    """
    Create the model and start the training
    """

    # Get the CL arguments
    args = get_arguments()

    # Check if the network architecture is valid
    if args.arch not in VALID_ARCHS:
        raise ValueError("Network architecture %s is not supported!"%(args.arch))

    # Check if the method to compute importance is valid
    if args.imp_method not in MODELS:
        raise ValueError("Importance measure %s is undefined!"%(args.imp_method))
    
    # Check if the optimizer is valid
    if args.optim not in VALID_OPTIMS:
        raise ValueError("Optimizer %s is undefined!"%(args.optim))

    # Create log directories to store the results
    if not os.path.exists(args.log_dir):
        print('Log directory %s created!'%(args.log_dir))
        os.makedirs(args.log_dir)

    # Generate the experiment key and store the meta data in a file
    exper_meta_data = {'DATASET': 'PERMUTE_MNIST',
            'NUM_RUNS': args.num_runs,
            'TRAIN_SINGLE_EPOCH': args.train_single_epoch, 
            'IMP_METHOD': args.imp_method, 
            'SYNAP_STGTH': args.synap_stgth,
            'FISHER_EMA_DECAY': args.fisher_ema_decay,
            'FISHER_UPDATE_AFTER': args.fisher_update_after,
            'OPTIM': args.optim, 
            'LR': args.learning_rate, 
            'BATCH_SIZE': args.batch_size, 
            'MEM_SIZE': args.mem_size}
    experiment_id = "PERMUTE_MNIST_HERDING_%s_%s_%s_%s_%r_%s-"%(args.arch, args.train_single_epoch, args.imp_method, str(args.synap_stgth).replace('.', '_'), 
            str(args.batch_size), str(args.mem_size)) + datetime.datetime.now().strftime("%y-%m-%d-%H-%M")
    snapshot_experiment_meta_data(args.log_dir, experiment_id, exper_meta_data)

    # Get the subset of data depending on training or cross-validation mode
    if args.online_cross_val:
        num_tasks = K_FOR_CROSS_VAL
    else:
        num_tasks = NUM_TASKS - K_FOR_CROSS_VAL

    # Variables to store the accuracies and standard deviations of the experiment
    acc_mean = dict()
    acc_std = dict()

    # Reset the default graph
    tf.reset_default_graph()
    graph  = tf.Graph()
    with graph.as_default():

        # Set the random seed
        tf.set_random_seed(args.random_seed)

        # Define Input and Output of the model
        x = tf.placeholder(tf.float32, shape=[None, INPUT_FEATURE_SIZE])
        y_ = tf.placeholder(tf.float32, shape=[None, TOTAL_CLASSES])
        learning_rate = tf.placeholder(dtype=tf.float32, shape=())
        if args.imp_method == 'ER-Hindsight-Anchors':
            anchor_xx = tf.Variable(tf.truncated_normal([1, INPUT_FEATURE_SIZE], mean=0.5, stddev=0.3), name='anchor_xx')
            anchor_points = tf.placeholder(tf.float32, shape=[None, INPUT_FEATURE_SIZE])
        else:
            anchor_xx = None
            anchor_points = None
        

        # Define the optimizer
        if args.optim == 'ADAM':
            opt = tf.train.AdamOptimizer(learning_rate=learning_rate)

        elif args.optim == 'SGD':
            opt = tf.train.GradientDescentOptimizer(learning_rate=learning_rate)

        elif args.optim == 'MOMENTUM':
            opt = tf.train.MomentumOptimizer(learning_rate, OPT_MOMENTUM)

        # Create the Model/ contruct the graph
        model = Model(x, y_, num_tasks, opt, args.imp_method, args.synap_stgth, args.fisher_update_after, 
                args.fisher_ema_decay, learning_rate, network_arch=args.arch, anchor_xx=anchor_xx, anchor_eta=args.anchor_eta, anchor_points=anchor_points)

        # Set up tf session and initialize variables.
        if USE_GPU:
            config = tf.ConfigProto()
            config.gpu_options.allow_growth = True
        else:
            config = tf.ConfigProto(
                    device_count = {'GPU': 0}
                    )

        time_start = time.time()
        with tf.Session(config=config, graph=graph) as sess:
            runs = train_task_sequence(model, sess, args)
            # Close the session
            sess.close()
        time_end = time.time()
        time_spent = time_end - time_start

    # Store all the results in one dictionary to process later
    exper_acc = dict(mean=runs)

    # If cross-validation flag is enabled, store the stuff in a text file
    if args.cross_validate_mode:
        acc_mean, acc_std = average_acc_stats_across_runs(runs, model.imp_method)
        fgt_mean, fgt_std = average_fgt_stats_across_runs(runs, model.imp_method)
        cross_validate_dump_file = args.log_dir + '/' + 'PERMUTE_MNIST_%s_%s'%(args.imp_method, args.optim) + '.txt'
        with open(cross_validate_dump_file, 'a') as f:
            if MULTI_TASK:
                f.write('GPU:{} \t ARCH: {} \t LR:{} \t LAMBDA: {} \t ACC: {}\n'.format(USE_GPU, args.arch, args.learning_rate, 
                    args.synap_stgth, acc_mean[-1, :].mean()))
            else:
                f.write('GPU: {} \t ARCH: {} \t LR:{} \t LAMBDA: {} \t ACC: {} (+-{})\t Fgt: {} (+-{})\t Time: {}\n'.format(USE_GPU, args.arch, args.learning_rate, 
                    args.synap_stgth, acc_mean, acc_std, fgt_mean, fgt_std, str(time_spent)))

    # Store the experiment output to a file
    snapshot_experiment_eval(args.log_dir, experiment_id, exper_acc)

if __name__ == '__main__':
    main()
