import cv2
import time
import os
import numpy as np
import logging
import functools
import tensorflow as tf
import lanms
import model
from typing import Tuple
from icdar import restore_rectangle

logging.getLogger('tensorflow').setLevel(logging.CRITICAL)
tf_logger = logging.getLogger('tensorflow')
tf_logger.disabled = True


class ImageProcessor:
    def __init__(self, argv):
        tesseract_config = {"l": argv["language"], "oem": argv["oem"], "psm": argv["psm"]}
        patterns = argv["patterns"]
        if patterns:
            tesseract_config["patterns"] = patterns

        self.verbose: int = argv["verbose"]
        self.checkpoint_path: str = argv['checkpoint_path']
        self.min_confidence: float = argv['min_confidence']
        self.padding: Tuple[float, float] = (argv['padding_horizontal'], argv['padding_vertical'])
        self.preview_mode: int = argv["preview_mode"]
        self.format: str = argv["format"]
        os.environ['CUDA_VISIBLE_DEVICES'] = argv["gpu_list"]

    @staticmethod
    def _resize_image(im, max_side_len=2400):
        """
        resize image to a size multiple of 32 which is required by the network
        :param im: the resized image
        :param max_side_len: limit of max image size to avoid out of memory in gpu
        :return: the resized image and the resize ratio
        """
        h, w, _ = im.shape

        resize_w = w
        resize_h = h

        # limit the max side
        if max(resize_h, resize_w) > max_side_len:
            ratio = float(max_side_len) / resize_h if resize_h > resize_w else float(max_side_len) / resize_w
        else:
            ratio = 1.
        resize_h = int(resize_h * ratio)
        resize_w = int(resize_w * ratio)

        resize_h = resize_h if resize_h % 32 == 0 else (resize_h // 32 - 1) * 32
        resize_w = resize_w if resize_w % 32 == 0 else (resize_w // 32 - 1) * 32
        resize_h = max(32, resize_h)
        resize_w = max(32, resize_w)
        im = cv2.resize(im, (int(resize_w), int(resize_h)))

        ratio_h = resize_h / float(h)
        ratio_w = resize_w / float(w)

        return im, (ratio_h, ratio_w)

    @staticmethod
    def _detect(score_map, geo_map, timer, score_map_thresh=0.8, box_thresh=0.1, nms_thres=0.2):
        """
        restore text boxes from score map and geo map
        :param score_map:
        :param geo_map:
        :param timer:
        :param score_map_thresh: threshhold for score map
        :param box_thresh: threshhold for boxes
        :param nms_thres: threshold for nms
        :return:
        """
        if len(score_map.shape) == 4:
            score_map = score_map[0, :, :, 0]
            geo_map = geo_map[0, :, :, ]
        # filter the score map
        xy_text = np.argwhere(score_map > score_map_thresh)
        # sort the text boxes via the y axis
        xy_text = xy_text[np.argsort(xy_text[:, 0])]
        # restore
        start = time.time()
        text_box_restored = restore_rectangle(xy_text[:, ::-1] * 4, geo_map[xy_text[:, 0], xy_text[:, 1], :])  # N*4*2
        print('{} text boxes before nms'.format(text_box_restored.shape[0]))
        boxes = np.zeros((text_box_restored.shape[0], 9), dtype=np.float32)
        boxes[:, :8] = text_box_restored.reshape((-1, 8))
        boxes[:, 8] = score_map[xy_text[:, 0], xy_text[:, 1]]
        timer['restore'] = time.time() - start
        # nms part
        start = time.time()
        # boxes = nms_locality.nms_locality(boxes.astype(np.float64), nms_thres)
        boxes = lanms.merge_quadrangle_n9(boxes.astype('float32'), nms_thres)
        timer['nms'] = time.time() - start

        if boxes.shape[0] == 0:
            return None, timer

        # here we filter some low score boxes by the average score map, this is different from the orginal paper
        for i, box in enumerate(boxes):
            mask = np.zeros_like(score_map, dtype=np.uint8)
            cv2.fillPoly(mask, box[:8].reshape((-1, 4, 2)).astype(np.int32) // 4, 1)
            boxes[i, 8] = cv2.mean(score_map, mask)[0]
        boxes = boxes[boxes[:, 8] > box_thresh]

        return boxes, timer

    @staticmethod
    def _sort_poly(p):
        min_axis = np.argmin(np.sum(p, axis=1))
        p = p[[min_axis, (min_axis + 1) % 4, (min_axis + 2) % 4, (min_axis + 3) % 4]]
        if abs(p[0, 0] - p[1, 0]) > abs(p[0, 1] - p[1, 1]):
            return p
        else:
            return p[[0, 3, 2, 1]]

    def text_detection(self, image: str):
        return self.get_predictor()(image)

    @functools.lru_cache(maxsize=100)
    def get_predictor(self):
        input_images = tf.placeholder(tf.float32, shape=[None, None, None, 3], name='input_images')
        global_step = tf.get_variable('global_step', [], initializer=tf.constant_initializer(0), trainable=False)

        f_score, f_geometry = model.model(input_images, is_training=False)

        variable_averages = tf.train.ExponentialMovingAverage(0.997, global_step)
        saver = tf.train.Saver(variable_averages.variables_to_restore())

        sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True))

        ckpt_state = tf.train.get_checkpoint_state(self.checkpoint_path)
        model_path = os.path.join(self.checkpoint_path, os.path.basename(ckpt_state.model_checkpoint_path))
        saver.restore(sess, model_path)

        def predictor(img):
            start_time = time.time()

            # rtparams['image_size'] = '{}x{}'.format(img.shape[1], img.shape[0])
            im = cv2.imread(img)[:, :, ::-1]
            im_resized, (ratio_h, ratio_w) = self._resize_image(im)

            timer = {'net': 0, 'restore': 0, 'nms': 0}
            start = time.time()
            score, geometry = sess.run([f_score, f_geometry], feed_dict={input_images: [im_resized]})
            timer['net'] = time.time() - start

            boxes, timer = self._detect(score_map=score, geo_map=geometry, timer=timer)
            print('{} : net {:.0f}ms, restore {:.0f}ms, nms {:.0f}ms'.format(
                img, timer['net'] * 1000, timer['restore'] * 1000, timer['nms'] * 1000))

            scores = None
            if boxes is not None:
                scores = boxes[:, 8].reshape(-1)
                boxes = boxes[:, :8].reshape((-1, 4, 2))
                boxes[:, :, 0] /= ratio_w
                boxes[:, :, 1] /= ratio_h

            duration = time.time() - start_time
            print('[timing] {}'.format(duration))

            # save to file
            if boxes is not None:
                for box, score in zip(boxes, scores):
                    # to avoid submitting errors
                    box = self._sort_poly(box.astype(np.int32))
                    if np.linalg.norm(box[0] - box[1]) < 5 or np.linalg.norm(box[3] - box[0]) < 5:
                        continue
                    print(box, score)
                    cv2.polylines(im[:, :, ::-1], [box.astype(np.int32).reshape((-1, 1, 2))], True,
                                  color=(0, 255, 0), thickness=2)

            title = os.path.basename(img)
            cv2.imshow(title, im[:, :, ::-1])
            cv2.waitKey(0)

        return predictor
