"""
MNIST example that uses logger which can communicate back to the hyperparameter optimization
service in Vertex AI (GCP).

Ref: https://cloud.google.com/vertex-ai/docs/training/using-hyperparameter-tuning

Requirements:
    Since Vertex tuning job is a serverless HPO orchestration service, the training script needs
    to be (1) dockerized and (2) accept input relevant arguments for hyperparameters (via argparse).
    Otherwise the communication between the outside of the container (i.e. Vertex GCP platform) and 
    the inside of the container (your script with current loss and accuracy values) is not possible.
    Assuming this very script is dockerized and is called as ENTRYPOINT from the Dockerfile, only a HPO
    config file is needed. The user can specify # of parallel runs, target metric, tunable hyperparameters,
    search algorithm and hardware spec.

    exemplary Dockerfile:
    ```
    FROM pytorch/pytorch:1.11.0-cuda11.3-cudnn8-runtime
    COPY mnist/ /mnist/

    RUN pip install pytorch-ignite pyyaml torchvision pynvml

    ENTRYPOINT ["python", "/mnist/mnist_vertex_hpo_logger.py"]
    ```

    see this example of `config_HPO.yaml`:
    ```
    studySpec:
    metrics:
    - metricId: nll
        goal: MINIMIZE
    parameters:
    - parameterId: lr
        scaleType: UNIT_LINEAR_SCALE
        doubleValueSpec:
        minValue: 0.000001
        maxValue: 0.001
    - parameterId: hp-tune
        categoricalValueSpec:
        values:
            - "y"
    algorithm: RANDOM_SEARCH
    trialJobSpec:
    workerPoolSpecs:
    - machineSpec:
        machineType: n1-standard-8
        acceleratorType: NVIDIA_TESLA_T4
        acceleratorCount: 1
        replicaCount: 1
        containerSpec:
        imageUri: gcr.io/<path-to-image-containing-this-script>:<tag>
    ```

 Usage:

    submit this script as a Vertex tuning job via:
    ```bash
        gcloud alpha ai hp-tuning-jobs create --display-name=my_tuning_job \
        --project=$PROJECT --region=$REGION --config=config_HPO.yaml \
        --max-trial-count=12 --parallel-trial-count=4
    ```

    for testing run this scrip on a regular machine and check whether `/tmp/hypertune/output.metrics` has been created
    ```bash
    python mnist_vertex_hpo_logger.py
    ```
"""

from argparse import ArgumentParser

import torch
import torch.nn.functional as F
from torch import nn
from torch.optim import SGD
from torch.utils.data import DataLoader
from torchvision.datasets import MNIST
from torchvision.transforms import Compose, Normalize, ToTensor
from tqdm import tqdm

from ignite.contrib.handlers.hpo_logger import HPOLogger

from ignite.engine import create_supervised_evaluator, create_supervised_trainer, Events
from ignite.metrics import Accuracy, Loss
from ignite.utils import setup_logger


class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
        self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
        self.conv2_drop = nn.Dropout2d()
        self.fc1 = nn.Linear(320, 50)
        self.fc2 = nn.Linear(50, 10)

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
        x = x.view(-1, 320)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, training=self.training)
        x = self.fc2(x)
        return F.log_softmax(x, dim=-1)


def get_data_loaders(train_batch_size, val_batch_size):
    data_transform = Compose([ToTensor(), Normalize((0.1307,), (0.3081,))])

    train_loader = DataLoader(
        MNIST(download=True, root=".", transform=data_transform, train=True), batch_size=train_batch_size, shuffle=True
    )

    val_loader = DataLoader(
        MNIST(download=False, root=".", transform=data_transform, train=False), batch_size=val_batch_size, shuffle=False
    )
    return train_loader, val_loader


def run(train_batch_size, val_batch_size, epochs, lr, momentum, log_interval):
    train_loader, val_loader = get_data_loaders(train_batch_size, val_batch_size)
    model = Net()
    device = "cpu"

    if torch.cuda.is_available():
        device = "cuda"

    model.to(device)  # Move model before creating optimizer
    optimizer = SGD(model.parameters(), lr=lr, momentum=momentum)
    criterion = nn.NLLLoss()
    trainer = create_supervised_trainer(model, optimizer, criterion, device=device)
    trainer.logger = setup_logger("trainer")

    val_metrics = {"accuracy": Accuracy(), "nll": Loss(criterion)}
    evaluator = create_supervised_evaluator(model, metrics=val_metrics, device=device)
    evaluator.logger = setup_logger("evaluator")

    pbar = tqdm(initial=0, leave=False, total=len(train_loader), desc=f"ITERATION - loss: {0:.2f}")

    @trainer.on(Events.ITERATION_COMPLETED(every=log_interval))
    def log_training_loss(engine):
        pbar.desc = f"ITERATION - loss: {engine.state.output:.2f}"
        pbar.update(log_interval)

    @trainer.on(Events.EPOCH_COMPLETED)
    def log_training_results(engine):
        pbar.refresh()
        evaluator.run(train_loader)
        metrics = evaluator.state.metrics
        avg_accuracy = metrics["accuracy"]
        avg_nll = metrics["nll"]
        tqdm.write(
            f"Training Results - Epoch: {engine.state.epoch} Avg accuracy: {avg_accuracy:.2f} Avg loss: {avg_nll:.2f}"
        )

    @trainer.on(Events.EPOCH_COMPLETED)
    def log_validation_results(engine):
        evaluator.run(val_loader)
        metrics = evaluator.state.metrics
        avg_accuracy = metrics["accuracy"]
        avg_nll = metrics["nll"]
        tqdm.write(
            f"Validation Results - Epoch: {engine.state.epoch} Avg accuracy: {avg_accuracy:.2f} Avg loss: {avg_nll:.2f}"
        )

        pbar.n = pbar.last_print_n = 0

    @trainer.on(Events.EPOCH_COMPLETED | Events.COMPLETED)
    def log_time(engine):
        tqdm.write(f"{trainer.last_event_name.name} took { trainer.state.times[trainer.last_event_name.name]} seconds")

    # instantiate Vertex HPO logger class
    hpo_logger = HPOLogger(evaluator=evaluator, metric_tag="nll")
    trainer.add_event_handler(Events.EPOCH_COMPLETED, hpo_logger)

    trainer.run(train_loader, max_epochs=epochs)
    pbar.close()


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--batch_size", type=int, default=64, help="input batch size for training (default: 64)")
    parser.add_argument(
        "--val_batch_size", type=int, default=1000, help="input batch size for validation (default: 1000)"
    )
    parser.add_argument("--epochs", type=int, default=10, help="number of epochs to train (default: 10)")
    parser.add_argument("--lr", type=float, default=0.01, help="learning rate (default: 0.01)")
    parser.add_argument("--momentum", type=float, default=0.5, help="SGD momentum (default: 0.5)")
    parser.add_argument(
        "--log_interval", type=int, default=10, help="how many batches to wait before logging training status"
    )

    args = parser.parse_args()

    run(args.batch_size, args.val_batch_size, args.epochs, args.lr, args.momentum, args.log_interval)
