import numpy as np
import torch
import torch.nn as nn

from backbone.cspdarknet import cspdarknet53
from utils.modules import Conv, UpSample, BottleneckCSP, DilatedEncoder
from utils import box_ops
from utils import loss


class YOLOv4(nn.Module):
    def __init__(self, 
                 device, 
                 img_size=640, 
                 num_classes=80, 
                 trainable=False, 
                 conf_thresh=0.001, 
                 nms_thresh=0.60, 
                 anchor_size=None):

        super(YOLOv4, self).__init__()
        self.device = device
        self.img_size = img_size
        self.num_classes = num_classes
        self.stride = [8, 16, 32]
        self.trainable = trainable
        self.conf_thresh = conf_thresh
        self.nms_thresh = nms_thresh
        self.anchor_size = torch.tensor(anchor_size).reshape(len(self.stride), len(anchor_size) // 3, 2).float()
        self.num_anchors = self.anchor_size.size(1)
        self.grid_cell, self.anchors_wh = self.create_grid(img_size)

        # backbone
        print('backbone: CSPDarkNet ...')
        self.backbone = cspdarknet53(pretrained=trainable)
        c3, c4, c5 = 256, 512, 1024

        # head
        self.head_conv_0 = DilatedEncoder(c5, c5//2)  # 10
        self.head_upsample_0 = UpSample(scale_factor=2)
        self.head_csp_0 = BottleneckCSP(c4 + c5//2, c4, n=3, shortcut=False)

        # P3/8-small
        self.head_conv_1 = Conv(c4, c4//2, k=1)  # 14
        self.head_upsample_1 = UpSample(scale_factor=2)
        self.head_csp_1 = BottleneckCSP(c3 + c4//2, c3, n=3, shortcut=False)

        # P4/16-medium
        self.head_conv_2 = Conv(c3, c3, k=3, p=1, s=2)
        self.head_csp_2 = BottleneckCSP(c3 + c4//2, c4, n=3, shortcut=False)

        # P8/32-large
        self.head_conv_3 = Conv(c4, c4, k=3, p=1, s=2)
        self.head_csp_3 = BottleneckCSP(c4 + c5//2, c5, n=3, shortcut=False)

        # det conv
        self.head_det_1 = nn.Conv2d(c3, self.num_anchors * (1 + self.num_classes + 4), 1)
        self.head_det_2 = nn.Conv2d(c4, self.num_anchors * (1 + self.num_classes + 4), 1)
        self.head_det_3 = nn.Conv2d(c5, self.num_anchors * (1 + self.num_classes + 4), 1)

        if self.trainable:
            # init bias
            self.init_bias()


    def init_bias(self):               
        # init bias
        init_prob = 0.01
        bias_value = -torch.log(torch.tensor((1. - init_prob) / init_prob))
        nn.init.constant_(self.head_det_1.bias[..., :self.num_anchors], bias_value)
        nn.init.constant_(self.head_det_2.bias[..., :self.num_anchors], bias_value)
        nn.init.constant_(self.head_det_3.bias[..., :self.num_anchors], bias_value)


    def create_grid(self, img_size):
        total_grid_xy = []
        total_anchor_wh = []
        w, h = img_size, img_size
        for ind, s in enumerate(self.stride):
            # generate grid cells
            fmp_w, fmp_h = w // s, h // s
            grid_y, grid_x = torch.meshgrid([torch.arange(fmp_h), torch.arange(fmp_w)])
            # [H, W, 2] -> [HW, 2]
            grid_xy = torch.stack([grid_x, grid_y], dim=-1).float().view(-1, 2)
            # [HW, 2] -> [1, HW, 1, 2]   
            grid_xy = grid_xy[None, :, None, :].to(self.device)
            # [1, HW, 1, 2]
            anchor_wh = self.anchor_size[ind].repeat(fmp_h*fmp_w, 1, 1).unsqueeze(0).to(self.device)

            total_grid_xy.append(grid_xy)
            total_anchor_wh.append(anchor_wh)

        return total_grid_xy, total_anchor_wh


    def set_grid(self, img_size):
        self.img_size = img_size
        self.grid_cell, self.anchors_wh = self.create_grid(img_size)


    def nms(self, dets, scores):
        """"Pure Python NMS YOLOv4."""
        x1 = dets[:, 0]  #xmin
        y1 = dets[:, 1]  #ymin
        x2 = dets[:, 2]  #xmax
        y2 = dets[:, 3]  #ymax

        areas = (x2 - x1) * (y2 - y1)                 # the size of bbox
        order = scores.argsort()[::-1]                        # sort bounding boxes by decreasing order

        keep = []                                             # store the final bounding boxes
        while order.size > 0:
            i = order[0]                                      #the index of the bbox with highest confidence
            keep.append(i)                                    #save it to keep
            # compute iou
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])

            w = np.maximum(1e-28, xx2 - xx1)
            h = np.maximum(1e-28, yy2 - yy1)
            inter = w * h

            ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-14)
            #reserve all the boundingbox whose ovr less than thresh
            inds = np.where(ovr <= self.nms_thresh)[0]
            order = order[inds + 1]

        return keep


    def postprocess(self, bboxes, scores):
        """
        bboxes: (HxW, 4), bsize = 1
        scores: (HxW, num_classes), bsize = 1
        """

        cls_inds = np.argmax(scores, axis=1)
        scores = scores[(np.arange(scores.shape[0]), cls_inds)]
        
        # threshold
        keep = np.where(scores >= self.conf_thresh)
        bboxes = bboxes[keep]
        scores = scores[keep]
        cls_inds = cls_inds[keep]

        # NMS
        keep = np.zeros(len(bboxes), dtype=np.int)
        for i in range(self.num_classes):
            inds = np.where(cls_inds == i)[0]
            if len(inds) == 0:
                continue
            c_bboxes = bboxes[inds]
            c_scores = scores[inds]
            c_keep = self.nms(c_bboxes, c_scores)
            keep[inds[c_keep]] = 1

        keep = np.where(keep > 0)
        bboxes = bboxes[keep]
        scores = scores[keep]
        cls_inds = cls_inds[keep]

        return bboxes, scores, cls_inds


    def forward(self, x, targets=None):
        B = x.size(0)
        KA = self.num_anchors
        C = self.num_classes
        # backbone
        c3, c4, c5 = self.backbone(x)

        # FPN + PAN
        # head
        c6 = self.head_conv_0(c5)
        c7 = self.head_upsample_0(c6)   # s32->s16
        c8 = torch.cat([c7, c4], dim=1)
        c9 = self.head_csp_0(c8)
        # P3/8
        c10 = self.head_conv_1(c9)
        c11 = self.head_upsample_1(c10)   # s16->s8
        c12 = torch.cat([c11, c3], dim=1)
        c13 = self.head_csp_1(c12)  # to det
        # p4/16
        c14 = self.head_conv_2(c13)
        c15 = torch.cat([c14, c10], dim=1)
        c16 = self.head_csp_2(c15)  # to det
        # p5/32
        c17 = self.head_conv_3(c16)
        c18 = torch.cat([c17, c6], dim=1)
        c19 = self.head_csp_3(c18)  # to det

        # det
        pred_s = self.head_det_1(c13)
        pred_m = self.head_det_2(c16)
        pred_l = self.head_det_3(c19)

        preds = [pred_s, pred_m, pred_l]
        obj_pred_list = []
        cls_pred_list = []
        box_pred_list = []

        for i, pred in enumerate(preds):
            # [B, KA*(1 + C + 4 + 1), H, W] -> [B, KA, H, W] -> [B, H, W, KA] ->  [B, HW*KA, 1]
            obj_pred_i = pred[:, :KA, :, :].permute(0, 2, 3, 1).contiguous().view(B, -1, 1)
            # [B, KA*(1 + C + 4 + 1), H, W] -> [B, KA*C, H, W] -> [B, H, W, KA*C] -> [B, H*W*KA, C]
            cls_pred_i = pred[:, KA:KA*(1+C), :, :].permute(0, 2, 3, 1).contiguous().view(B, -1, C)
            # [B, KA*(1 + C + 4 + 1), H, W] -> [B, KA*4, H, W] -> [B, H, W, KA*4] -> [B, HW, KA, 4]
            reg_pred_i = pred[:, KA*(1+C):, :, :].permute(0, 2, 3, 1).contiguous().view(B, -1, KA, 4)
            # txtytwth -> xywh
            xy_pred_i = (reg_pred_i[..., :2].sigmoid() * 2.0 - 1.0 + self.grid_cell[i]) * self.stride[i]
            wh_pred_i = reg_pred_i[..., 2:].exp() * self.anchors_wh[i]
            xywh_pred_i = torch.cat([xy_pred_i, wh_pred_i], dim=-1).view(B, -1, 4)
            # xywh -> x1y1x2y2
            x1y1_pred_i = xywh_pred_i[..., :2] - xywh_pred_i[..., 2:] / 2
            x2y2_pred_i = xywh_pred_i[..., :2] + xywh_pred_i[..., 2:] / 2
            box_pred_i = torch.cat([x1y1_pred_i, x2y2_pred_i], dim=-1)

            obj_pred_list.append(obj_pred_i)
            cls_pred_list.append(cls_pred_i)
            box_pred_list.append(box_pred_i)
        
        obj_pred = torch.cat(obj_pred_list, dim=1)
        cls_pred = torch.cat(cls_pred_list, dim=1)
        box_pred = torch.cat(box_pred_list, dim=1)
        
        # train
        if self.trainable:
            # decode bbox: [B, HW*KA, 4]
            x1y1x2y2_pred = (box_pred / self.img_size).view(-1, 4)
            x1y1x2y2_gt = targets[..., -4:].view(-1, 4)

            # giou: [B, HW*KA,]
            giou_pred = box_ops.giou_score(x1y1x2y2_pred, x1y1x2y2_gt, batch_size=B)

            # we set iou_pred as the target of the objectness prediction
            targets = torch.cat([0.5 * (giou_pred.view(B, -1, 1).clone().detach() + 1.0), targets], dim=-1)

            # loss
            obj_loss, cls_loss, reg_loss, total_loss = loss.loss(pred_obj=obj_pred,
                                                                  pred_cls=cls_pred,
                                                                  pred_giou=giou_pred,
                                                                  targets=targets)

            return obj_loss, cls_loss, reg_loss, total_loss

        # test
        else:
            with torch.no_grad():
                # batch size = 1
                # [B, H*W*KA, C] -> [H*W*KA, C]
                scores = torch.sigmoid(obj_pred)[0] * torch.softmax(cls_pred, dim=-1)[0]
                # [B, H*W*KA, 4] -> [H*W*KA, 4]
                bboxes = torch.clamp((box_pred / self.img_size)[0], 0., 1.)

                # to cpu
                scores = scores.to('cpu').numpy()
                bboxes = bboxes.to('cpu').numpy()

                # post-process
                bboxes, scores, cls_inds = self.postprocess(bboxes, scores)

                return bboxes, scores, cls_inds
