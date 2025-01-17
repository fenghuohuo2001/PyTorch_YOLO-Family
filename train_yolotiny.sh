python train.py \
        --cuda \
        -v yolo_tiny \
        -d coco \
        --root /mnt/share/ssd2/dataset \
        --batch_size 16 \
        --img_size 512 \
        --multi_scale_range 10 16 \
        -ms \
        --ema \
        --max_epoch 200 \
        --lr_epoch 100 150 \
        --mosaic \
        --center_sample
        