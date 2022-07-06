import json
import logging
import os
import sys
from shutil import copyfile
from matplotlib.style import use

import numpy as np
import torch.optim as optim
import torch.utils.data
from tensorboard_logger import configure, log_value
from torch.autograd import Variable
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data import DataLoader

from read_config import Config
from src.dataset import DataSetControlPointsPoisson
from src.dataset import generator_iter
from src.loss import (
    control_points_permute_reg_loss,
)
from src.loss import laplacian_loss
from src.loss import (
    uniform_knot_bspline,
    spline_reconstruction_loss_one_sided,
)
from src.model import DGCNNControlPoints
from src.utils import rescale_input_outputs

np.set_printoptions(precision=4)

config = Config(sys.argv[1])
# print("<<<<<<<<<<<<<<--------------------------------------->>>>>>>>>>>>>>>>>>>>>>>>>>")
# print(sys.argv[1])


# model name is like an string statement where file name is stored in a suitable and required formate
model_name = config.model_path.format(
    config.mode,
    config.num_points,
    config.loss_weight,
    config.batch_size,
    config.lr,
    config.num_train,
    config.num_test,
    config.loss_weight,
)

print("Model name: ", model_name)
print(config.config)

# present folder directory
userspace = os.path.dirname(os.path.abspath(__file__))
# print("<<<<<<<<<<<<<<<<<<<<<<<---------------------------------->>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
# print(userspace)

# create a file on logs/tensorboard of a model_name formate
configure("logs/tensorboard/{}".format(model_name), flush_secs=5)


# It will write error logs on a file so that we can go through with this latterly
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("%(asctime)s:%(name)s:%(message)s")

# Location of error log file
file_handler = logging.FileHandler(
    "logs/logs/{}.log".format(model_name), mode="w"
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(handler)


# write error logs in json file in particular formate
with open(
        "logs/configs/{}_config.json".format(model_name), "w"
) as file:
    json.dump(vars(config), file)

source_file = __file__
destination_file = "../logs_curve_fitting/scripts/{}_{}".format(
    model_name, __file__.split("/")[-1]
)

# Make a copy of this python file in the location "../logs_curve_fitting/scripts"
copyfile(source_file, destination_file)


control_decoder = DGCNNControlPoints(20, num_points=10, mode=config.mode)

# if torch.cuda.device_count() > 1:
control_decoder = torch.nn.DataParallel(control_decoder)
# control_decoder.cuda()

split_dict = {"train": config.num_train,
              "val": config.num_val, "test": config.num_test}

align_canonical = True
anisotropic = True
if_augment = True

dataset = DataSetControlPointsPoisson(
    config.dataset_path,  # 'data/spline/open_splines.h5'
    config.batch_size,  # 36
    splits=split_dict,
    size_v=config.grid_size,  # 20
    size_u=config.grid_size)  # 20

get_train_data = dataset.load_train_data(
    if_regular_points=True, align_canonical=align_canonical, anisotropic=anisotropic, if_augment=if_augment
)

get_val_data = dataset.load_val_data(
    if_regular_points=True, align_canonical=align_canonical, anisotropic=anisotropic
)

loader = generator_iter(get_train_data, int(1e10))
get_train_data = iter(
    DataLoader(
        loader,
        batch_size=1,
        shuffle=False,
        collate_fn=lambda x: x,
        num_workers=0,
        pin_memory=False,
    )
)

loader = generator_iter(get_val_data, int(1e10))
get_val_data = iter(
    DataLoader(
        loader,
        batch_size=1,
        shuffle=False,
        collate_fn=lambda x: x,
        num_workers=0,
        pin_memory=False,
    )
)

optimizer = optim.Adam(control_decoder.parameters(), lr=config.lr)

scheduler = ReduceLROnPlateau(
    optimizer, mode="min", factor=0.5, patience=10, verbose=True, min_lr=3e-5
)

nu, nv = uniform_knot_bspline(20, 20, 3, 3, 40)
nu = torch.from_numpy(nu.astype(np.float32))  # .cuda()
nv = torch.from_numpy(nv.astype(np.float32))  # .cuda()

prev_test_cd = 1e8
for e in range(config.epochs):
    train_reg = []
    train_str = []
    train_cd = []
    train_lap = []
    control_decoder.train()
    for train_b_id in range(config.num_train // config.batch_size):
        optimizer.zero_grad()
        # torch.cuda.empty_cache()
        points_, parameters, control_points, scales, _ = next(get_train_data)[
            0]
        control_points = Variable(
            torch.from_numpy(control_points.astype(np.float32))
        )  # .cuda()

        points = Variable(torch.from_numpy(
            points_.astype(np.float32)))  # .cuda()
        points = points.permute(0, 2, 1)

        # Sample random number of points to make network robust to density.
        rand_num_points = config.num_points + \
            np.random.choice(np.arange(-300, 1300), 1)[0]

        output = control_decoder(points[:, :, 0:rand_num_points])
        # print("<<<<<<<<<<-------------------->>>>>>>>>>>>>>>>>>>>>>>>> output <<<<<<<<---------------->>>>>>>>>>>>")
        # print(output)

        if anisotropic:
            # rescale all tensors to original dimensions for evaluation
            scales, output, points, control_points = rescale_input_outputs(scales, output, points, control_points,
                                                                           config.batch_size)

        # Chamfer Distance loss, between predicted and GT surfaces
        cd, reconstructed_points = spline_reconstruction_loss_one_sided(
            nu, nv, output, points, config
        )

        # Permutation Regression Loss
        # permute_cp has the best permutation of gt control points grid
        l_reg, permute_cp = control_points_permute_reg_loss(
            output, control_points, config.grid_size
        )

        laplac_loss = laplacian_loss(
            output.reshape(
                (config.batch_size, config.grid_size, config.grid_size, 3)),
            permute_cp,
            dist_type="l2",
        )

        loss = l_reg * config.loss_weight + (cd + laplac_loss) * (
            1 - config.loss_weight
        )

        loss.backward()
        train_cd.append(cd.data.numpy())
        train_reg.append(l_reg.data.numpy())
        train_lap.append(laplac_loss.data.numpy())
        optimizer.step()
        log_value(
            "cd",
            cd.data.numpy(),
            train_b_id + e * (config.num_train // config.batch_size),
        )
        log_value(
            "l_reg",
            l_reg.data.numpy(),
            train_b_id + e * (config.num_train // config.batch_size),
        )

        log_value(
            "l_lap",
            laplac_loss.data.numpy(),
            train_b_id + e * (config.num_train // config.batch_size),
        )
        print(
            "\rEpoch: {} iter: {}, loss: {}".format(
                e, train_b_id, loss.item()
            ),
            end="",
        )

    distances = []
    test_reg = []
    test_cd = []
    test_str = []
    test_lap = []
    control_decoder.eval()

    for val_b_id in range(config.num_test // config.batch_size - 1):
        torch.cuda.empty_cache()
        points_, parameters, control_points, scales, _ = next(get_val_data)[0]

        control_points = Variable(
            torch.from_numpy(control_points.astype(np.float32))
        )  # .cuda()
        points = Variable(torch.from_numpy(
            points_.astype(np.float32)))  # .cuda()
        points = points.permute(0, 2, 1)
        with torch.no_grad():
            output = control_decoder(points[:, :, 0:config.num_points])
            if anisotropic:
                scales, output, points, control_points = rescale_input_outputs(scales, output, points, control_points,
                                                                               config.batch_size)

                # Chamfer Distance loss, between predicted and GT surfaces
        cd, reconstructed_points = spline_reconstruction_loss_one_sided(
            nu, nv, output, points, config
        )

        l_reg, permute_cp = control_points_permute_reg_loss(
            output, control_points, config.grid_size
        )
        laplac_loss = laplacian_loss(
            output.reshape(
                (config.batch_size, config.grid_size, config.grid_size, 3)),
            permute_cp,
            dist_type="l2",
        )

        loss = l_reg * config.loss_weight + (cd + laplac_loss) * (
            1 - config.loss_weight
        )
        test_reg.append(l_reg.data.numpy())
        test_cd.append(cd.data.numpy())
        test_lap.append(laplac_loss.data.numpy())

    print("\n")
    logger.info(
        "Epoch: {}/{} => Tr lreg: {}, Ts loss: {}, Tr CD: {}, Ts CD: {},  Tr lap: {}, Ts lap: {}".format(
            e,
            config.epochs,
            np.mean(train_reg),
            np.mean(test_reg),
            np.mean(train_cd),
            np.mean(test_cd),
            np.mean(train_lap),
            np.mean(test_lap),
        )
    )

    log_value("train_cd", np.mean(train_cd), e)
    log_value("test_cd", np.mean(test_cd), e)
    log_value("train_reg", np.mean(train_reg), e)
    log_value("test_reg", np.mean(test_reg), e)

    scheduler.step(np.mean(test_cd))
    if prev_test_cd > np.mean(test_cd):
        logger.info("CD improvement, saving model at epoch: {}".format(e))
        prev_test_cd = np.mean(test_cd)
        torch.save(
            control_decoder.state_dict(),
            "logs/trained_models/{}.pth".format(model_name),
        )
