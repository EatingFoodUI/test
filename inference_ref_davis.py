import numpy as np
import torch
import torch.nn as nn
from torch.nn import functional as F
import os
from tqdm import tqdm
import argparse
import warnings
from PIL import Image
import torchvision.transforms as transforms
import json
import logging
import cv2

import opts
import refersam
import loss

import random
seed = 42
torch.manual_seed(seed)
np.random.seed(seed)
random.seed(seed)
warnings.filterwarnings('ignore')


def main():
    # init opts
    args = opts.get_arguments()
    args.data = '../ref-davis/valid'
    print("Args:", args)

    # create output path
    output_path = './outputs/' + args.outdir
    if not os.path.exists('./outputs/'):
        os.mkdir('./outputs/')
    if not os.path.exists(output_path):
        os.mkdir(output_path)
    # load_dir = './outputs/' + args.load_dir

    if not os.path.exists('./outputs/'):
        os.mkdir('./outputs/')
    if not os.path.exists(output_path):
        os.mkdir(output_path)

    # init logger
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        filename=os.path.join(output_path, 'infer_log.txt'),
        filemode='w',
    )

    logger = logging.getLogger()
    console_handler = logging.StreamHandler()
    logger.addHandler(console_handler)

    # get palette
    palette_img = os.path.join("../ref-davis/valid/Annotations/cows/00000.png")
    palette = Image.open(palette_img).getpalette()

    # assert device
    device_count = torch.cuda.device_count()
    assert device_count == 1, "inference only use 1 gpu!"
    logger.info("device_count is {}".format(device_count))

    # load model
    logger.info("======> load model")
    text_model_name = args.text_encoder
    model = refersam.Model(args, text_model_name, logger).to('cuda')

    # load checkpoint
    if args.pretrain:
        if args.proj_mlp:
            model.resizer.load_state_dict(torch.load(args.pretrain_mlp))
        if args.dense_embeddings:
            model.dense_conv.load_state_dict(torch.load(args.pretrain_dense_conv))
            model.dense_conv2.load_state_dict(torch.load(args.pretrain_dense_conv2))
            model.fpn_conv.load_state_dict(torch.load(args.pretrain_fpn))
        if args.train_decoder:
            model.sam.mask_decoder.load_state_dict(torch.load(args.pretrain_decoder))
        if args.pretrain_track_token_mlp != "0":
            model.track_token_mlp.load_state_dict(torch.load(args.pretrain_track_token_mlp))
        if args.pretrain_adapter != "0":
            model.sam.image_encoder.load_state_dict(torch.load(args.pretrain_adapter, map_location='cpu'))

    model.eval()

    # load data
    logger.info("load ref-davis data")
    davis_valid_path = "../ref-davis/valid"
    meta_file = "../ref-davis/meta_expressions/valid/meta_expressions.json"
    with open(meta_file, "r") as f:
        data = json.load(f)["videos"]
    video_list = list(data.keys())
    video_num = len(video_list)

    transform = transforms.Compose([
        transforms.Resize(360),
        transforms.ToTensor(),
        # transforms.Resize(360),
        # T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])

    # inference
    logger.info('Start inference')
    # 1. for each video
    for video in tqdm(video_list):

        metas = []

        expressions = data[video]["expressions"]
        expression_list = list(expressions.keys())
        num_expressions = len(expression_list)
        video_len = len(data[video]["frames"])

        # read all the anno meta
        for i in range(num_expressions):
            meta = {}
            meta["video"] = video
            meta["exp"] = expressions[expression_list[i]]["exp"]
            meta["exp_id"] = expression_list[i] # start from 0
            meta["frames"] = data[video]["frames"]
            metas.append(meta)
        meta = metas

        # since there are 4 annotations
        num_obj = num_expressions // 4

        ## init track token
        track_tokens_list = []  # for first frame
        for i in range(num_obj):
            track_tokens_list.append(None)

        # 2. for each clip's frames
        for clip_id in range(0, video_len, 1):
            frames_ids = [x for x in range(video_len)]
            clip_frames_ids = frames_ids[clip_id: clip_id + 1]
            clip_len = len(clip_frames_ids)

            # load current image
            cur_idx = '%05d' % int(clip_id)
            cur_image_path = os.path.join(args.data, "JPEGImages", video, cur_idx + ".jpg")
            cur_image = cv2.imread(cur_image_path)
            cur_image = cv2.cvtColor(cur_image, cv2.COLOR_BGR2RGB)

            # for each annotator
            # for anno_id in range(4):
            # test for one annotator
            for anno_id in range(4):
                cur_output_path = os.path.join(output_path, str(anno_id))
                if not os.path.exists(cur_output_path):
                    os.mkdir(cur_output_path)
                cur_video_output_path = os.path.join(cur_output_path, video)
                if not os.path.exists(cur_video_output_path):
                    os.mkdir(cur_video_output_path)

                # plt.figure(figsize=(10, 10))
                # plt.imshow(cur_image)
                cur_mask_list = []
                for obj_id in range(num_obj):
                    i = obj_id * 4 + anno_id
                    exp = meta[i]["exp"]
                    exp_id = meta[i]["exp_id"]

                    cur_track_tokens = track_tokens_list[obj_id]

                    target = {
                        'caption': exp,
                        'img': [cur_image],
                        'track_tokens': [cur_track_tokens],
                    }

                    with torch.no_grad():
                        output, update_track_tokens = model([cur_image], exp, target, [cur_track_tokens])

                    cur_mask_list.append(output[0][0]) # [1, 480, 910]

                    track_tokens_list[obj_id] = update_track_tokens[0]

                # save masks
                # 1. masks < 0的全部设置为0
                for index, one_mask in enumerate(cur_mask_list):
                    # cur_mask_list[index][cur_mask_list[index] < 0.01] = 0
                    cur_mask_list[index][cur_mask_list[index] < 0] = 0
                # cur_mask_list = torch.Tensor(cur_mask_list).squeeze(1)
                cur_mask_list = torch.stack(cur_mask_list).squeeze(1)
                # 2. 添加背景
                background = 0.1 * torch.ones(1, cur_mask_list[0].shape[0], cur_mask_list[0].shape[1])
                background = background.to(torch.device(cur_mask_list.device))
                # 3. concate
                cur_mask_list = torch.cat([background, cur_mask_list], dim=0)
                # 4. 比较
                out_masks = torch.argmax(cur_mask_list, dim=0)

                out_masks = out_masks.detach().cpu().numpy().astype(np.uint8)  # [video_len, h, w] [h, w]
                # 5. 保存
                mask_output_path = os.path.join(cur_video_output_path, cur_idx + '.png')
                img_E = Image.fromarray(out_masks)
                img_E.putpalette(palette)
                img_E.save(mask_output_path)



if __name__ == "__main__":
    main()