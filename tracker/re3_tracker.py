import cv2
import glob
import numpy as np
import os
import scipy
import tensorflow as tf
import time

import sys
import os.path
sys.path.append(os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.path.pardir)))

import network

from re3_utils.util import bb_util
from re3_utils.util import im_util
from re3_utils.tensorflow_util import tf_util

# Network Constants
from constants import CROP_SIZE
from constants import CROP_PAD
from constants import LSTM_SIZE
from constants import OUTPUT_SIZE
from constants import LOG_DIR
from constants import GPU_ID
from constants import MAX_TRACK_LENGTH

os.environ['CUDA_VISIBLE_DEVICES'] = str(GPU_ID)

SPEED_OUTPUT = False

class Re3Tracker(object):
    def __init__(self, gpu_id=None):
        if gpu_id is not None:
            os.environ['CUDA_VISIBLE_DEVICES'] = str(GPU_ID)
        basedir = os.path.dirname(__file__)
        tf.Graph().as_default()
        self.targetImagePlaceholder = tf.placeholder(tf.float32, shape=(None, CROP_SIZE, CROP_SIZE, 3))
        self.prevLstmState = tuple([tf.placeholder(tf.float32, shape=(None, LSTM_SIZE)) for _ in xrange(4)])
        self.batch_size = tf.placeholder(tf.int32, shape=())
        self.targetOutputs, self.state1, self.state2 = network.inference(
                self.targetImagePlaceholder, num_unrolls=1, batch_size=self.batch_size, train=False,
                prevLstmState=self.prevLstmState)
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=config)
        ckpt = tf.train.get_checkpoint_state(os.path.join(basedir, '..', LOG_DIR, 'checkpoints'))
        tf_util.restore(self.sess, ckpt.model_checkpoint_path)

        self.tracked_data = {}

        self.time = 0
        self.total_forward_count = -1


    # unique_id{str}: A unique id for the object being tracked.
    # image{str or numpy array}: The current image or the path to the current image.
    # starting_box{None or 4x1 numpy array or list}: 4x1 bounding box in X1, Y1, X2, Y2 format.
    def track(self, unique_id, image, starting_box=None):
        start_time = time.time()

        if type(image) == str:
            image = scipy.misc.imread(image)
        else:
            image = image.copy()
        if starting_box is not None:
            lstmState = [np.zeros((1, LSTM_SIZE)) for _ in xrange(4)]
            pastBBox = np.array(starting_box) # turns list into numpy array if not and copies for safety.
            prevImage = image
            originalFeatures = None
            forwardCount = 0
        elif unique_id in self.tracked_data:
            lstmState, pastBBox, prevImage, originalFeatures, forwardCount = self.tracked_data[unique_id]
        else:
            raise Exception('Unique_id %s with no initial bounding box' % unique_id)

        croppedInput0, pastBBoxPadded = im_util.get_cropped_input(prevImage, pastBBox, CROP_PAD, CROP_SIZE)
        croppedInput1,_ = im_util.get_cropped_input(image, pastBBox, CROP_PAD, CROP_SIZE)

        feed_dict = {
                self.targetImagePlaceholder : [croppedInput0, croppedInput1],
                self.prevLstmState : lstmState,
                self.batch_size : 1,
                }
        rawOutput, s1, s2 = self.sess.run([self.targetOutputs, self.state1, self.state2], feed_dict=feed_dict)
        lstmState = [s1[0], s1[1], s2[0], s2[1]]
        if forwardCount == 0:
            originalFeatures = [s1[0], s1[1], s2[0], s2[1]]

        prevImage = image

        outputBox = (rawOutput / 10.0) * OUTPUT_SIZE * CROP_PAD

        # Shift output box to full image coordinate system.
        pastBoxXYWH = bb_util.xyxy_to_xywh(pastBBoxPadded)
        outputBox = (rawOutput.squeeze() / 10.0) * pastBoxXYWH[[2,3,2,3]]
        outputBox += pastBBoxPadded[[0,1,0,1]]
        outputBox = bb_util.clip_bbox(outputBox, 0, image.shape[1], image.shape[0])

        if forwardCount > 0 and forwardCount % MAX_TRACK_LENGTH == 0:
            croppedInput, _ = im_util.get_cropped_input(image, outputBox, CROP_PAD, CROP_SIZE)
            input = np.tile(croppedInput[np.newaxis,...], (2,1,1,1))
            feed_dict = {
                    self.targetImagePlaceholder : input,
                    self.prevLstmState : originalFeatures,
                    self.batch_size : 1,
                    }
            rawOutput, s1, s2 = self.sess.run([self.targetOutputs, self.state1, self.state2], feed_dict=feed_dict)
            lstmState = [s1[0], s1[1], s2[0], s2[1]]

        forwardCount += 1
        self.total_forward_count += 1

        if starting_box is not None:
            # Use label if it's given
            outputBox = np.array(starting_box)

        self.tracked_data[unique_id] = (lstmState, outputBox, image, originalFeatures, forwardCount)
        end_time = time.time()
        if self.total_forward_count > 0:
            self.time += (end_time - start_time)
        if SPEED_OUTPUT and self.total_forward_count % 100 == 0:
            print 'Current tracking speed: %.3f FPS' % (1 / (end_time - start_time))
            print 'Mean tracking speed:    %.3f FPS\n' % (self.total_forward_count / max(.00001, self.time))
        return outputBox


    # unique_ids{list{string}}: A list of unique ids for the objects being tracked.
    # image{str or numpy array}: The current image or the path to the current image.
    # starting_boxes{None or dictionary of unique_id to 4x1 numpy array or list}: unique_ids to starting box.
    #    Starting boxes only need to be provided if it is a new track. Bounding boxes in X1, Y1, X2, Y2 format.
    def multi_track(self, unique_ids, image, starting_boxes=None):
        start_time = time.time()
        assert type(unique_ids) == list, 'unique_ids must be a list for multi_track'
        assert len(unique_ids) > 1, 'unique_ids must be at least 2 elements'

        if type(image) == str:
            image = scipy.misc.imread(image)
        else:
            image = image.copy()

        # Get inputs for each track.
        images = []
        lstmStates = [[] for _ in xrange(4)]
        pastBBoxesPadded = []
        if starting_boxes is None:
            starting_boxes = dict()
        for unique_id in unique_ids:
            if unique_id in starting_boxes:
                lstmState = [np.zeros((1, LSTM_SIZE)) for _ in xrange(4)]
                pastBBox = np.array(starting_boxes[unique_id]) # turns list into numpy array if not and copies for safety.
                prevImage = image
                originalFeatures = None
                forwardCount = 0
                self.tracked_data[unique_id] = (lstmState, pastBBox, image, originalFeatures, forwardCount)
            elif unique_id in self.tracked_data:
                lstmState, pastBBox, prevImage, originalFeatures, forwardCount = self.tracked_data[unique_id]
            else:
                raise Exception('Unique_id %s with no initial bounding box' % unique_id)

            croppedInput0, pastBBoxPadded = im_util.get_cropped_input(prevImage, pastBBox, CROP_PAD, CROP_SIZE)
            croppedInput1,_ = im_util.get_cropped_input(image, pastBBox, CROP_PAD, CROP_SIZE)
            pastBBoxesPadded.append(pastBBoxPadded)
            images.extend([croppedInput0, croppedInput1])
            for ss,state in enumerate(lstmState):
                lstmStates[ss].append(state.squeeze())

        lstmStateArrays = []
        for state in lstmStates:
            lstmStateArrays.append(np.array(state))

        feed_dict = {
                self.targetImagePlaceholder : images,
                self.prevLstmState : lstmStateArrays,
                self.batch_size : len(images) / 2
                }
        rawOutput, s1, s2 = self.sess.run([self.targetOutputs, self.state1, self.state2], feed_dict=feed_dict)
        outputBoxes = np.zeros((len(unique_ids), 4))
        for uu,unique_id in enumerate(unique_ids):
            lstmState, pastBBox, prevImage, originalFeatures, forwardCount = self.tracked_data[unique_id]
            lstmState = [s1[0][[uu],:], s1[1][[uu],:], s2[0][[uu],:], s2[1][[uu],:]]
            if forwardCount == 0:
                originalFeatures = [s1[0][[uu],:], s1[1][[uu],:], s2[0][[uu],:], s2[1][[uu],:]]

            prevImage = image

            outputBox = (rawOutput[uu,:] / 10.0) * OUTPUT_SIZE * CROP_PAD

            # Shift output box to full image coordinate system.
            pastBBoxPadded = pastBBoxesPadded[uu]
            pastBoxXYWH = bb_util.xyxy_to_xywh(pastBBoxPadded)
            outputBox = (rawOutput[uu,:].squeeze() / 10.0) * pastBoxXYWH[[2,3,2,3]]
            outputBox += pastBBoxPadded[[0,1,0,1]]
            outputBox = bb_util.clip_bbox(outputBox, 0, image.shape[1], image.shape[0])

            if forwardCount > 0 and forwardCount % MAX_TRACK_LENGTH == 0:
                croppedInput, _ = im_util.get_cropped_input(image, outputBox, CROP_PAD, CROP_SIZE)
                input = np.tile(croppedInput[np.newaxis,...], (2,1,1,1))
                feed_dict = {
                        self.targetImagePlaceholder : input,
                        self.prevLstmState : originalFeatures,
                        self.batch_size : 1,
                        }
                _, s1_new, s2_new = self.sess.run([self.targetOutputs, self.state1, self.state2], feed_dict=feed_dict)
                lstmState = [s1_new[0], s1_new[1], s2_new[0], s2_new[1]]

            forwardCount += 1
            self.total_forward_count += 1

            if unique_id in starting_boxes:
                # Use label if it's given
                outputBox = np.array(starting_boxes[unique_id])

            outputBoxes[uu,:] = outputBox
            self.tracked_data[unique_id] = (lstmState, outputBox, image, originalFeatures, forwardCount)
        end_time = time.time()
        if self.total_forward_count > 0:
            self.time += (end_time - start_time)
        if SPEED_OUTPUT and self.total_forward_count % 100 == 0:
            print 'Current tracking speed: %.3f FPS per object' % (len(unique_ids) / (end_time - start_time))
            print 'Current tracking speed: %.3f FPS per frame' % (1 / (end_time - start_time))
            print 'Mean tracking speed:    %.3f FPS per object\n' % (self.total_forward_count / max(.00001, self.time))
        return outputBoxes



