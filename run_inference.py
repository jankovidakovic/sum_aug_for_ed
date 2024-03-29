import json
import logging
import os
from argparse import ArgumentParser
from pprint import pformat, pprint

import numpy as np
import pandas as pd
from sklearn.metrics import classification_report
from tqdm import tqdm
from transformers import pipeline

import wandb

from src.constants import MODEL_CLASSES
from src.data import DoceeForInference

logger = logging.getLogger(__name__)


def get_parser() -> ArgumentParser:
    parser = ArgumentParser()
    parser.add_argument(
        "--model_type",
        type=str,
        required=True,
        choices=MODEL_CLASSES.keys(),
        help="Type of the model to use. Model type uniquely identifies types "
             "of config, tokenizer and model."
    )
    parser.add_argument(
        "--pretrained_model_name_or_path",
        type=str,
        required=True,
        help="Path to pretrained model, a model checkpoint, or the "
             "name of a model which is available in Huggingface Model Hub."
    )
    parser.add_argument(
        "--test_filename",
        type=str,
        required=True,
        help="Filesystem path to a file containing the test set."
    )

    # parser.add_argument(
    #     "--num_labels",
    #     type=int,
    #     default=None,
    #     required=True,
    #     help="Number of unique labels in dataset. If not provided, will "
    #          "be calculated as the number of unique values in labels column "
    #          "in the training dataset. Name of the column containig labels can "
    #          "be set using the '--label_column' option.",
    # )
    #   TODO - im not sure if i need those

    # hyperparams
    parser.add_argument(
        "--do_lower_case",
        action="store_true",
        help="If set, input text will be lowercased during tokenization. "
             "This flag is useful when one is using uncased models (e.g. 'bert-base-uncased')",
    )

    parser.add_argument(
        "--batch_size",
        default=2,
        type=int,
        help="Batch size used during inference. Defaults to 2.",
    )
    parser.add_argument(
        "--num_workers",
        type=int,
        default=1,
        help="Number of workers to use for dataloading. Defaults to 1."
    )

    parser.add_argument(
        "--output_path",
        type=str,
        required=True,
        help="The output path to which the classification report will be saved."
    )

    # runtime meta args
    parser.add_argument(
        "--device",
        type=int,
        required=True,
        help="Device to use during inference. -1 defaults to CPU, while >=0 specifies a GPU number."
    )

    parser.add_argument(  # TODO - make use of this
        "--seed",
        type=int,
        default=42,
        help="Random seed. Will be used to perform dataset splitting, as well as "
             "random parameter initialization within the model. Defaults to 42."
    )

    # parser.add_argument(
    #     "--wandb_project",
    #     type=str,
    #     required=True,
    #     help="Project name to use for W&B logging."
    # )
    # parser.add_argument(
    #     "--wandb_run",
    #     type=str,
    #     required=True,
    #     help="Run name used to associated the experiment with W&B run."
    # )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="If set, logging level is set to INFO. Else, it's set to WARN."
    )

    parser.add_argument(
        "--use_title",
        action="store_true",
        help="If set, title will be prepended to DocEE examples during inference."
    )

    return parser


def main():
    args = get_parser().parse_args()

    logging.root.handlers = []
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        # log info only on main process
        level=logging.INFO if args.verbose else logging.WARN,
        handlers=[
            logging.StreamHandler(),
        ],
    )

    # wandb_run = wandb.init(
    #     project=args.wandb_project,
    #     entity="jankovidakovic",
    #     name=args.wandb_run
    # )

    logger.info(f"Using CUDA devices: {args.device}")

    # setup config, tokenizer and model
    model_type = MODEL_CLASSES[args.model_type]
    logger.info(f"Using model type: {args.model_type}")

    tokenizer = model_type.tokenizer.from_pretrained(
        pretrained_model_name_or_path=args.pretrained_model_name_or_path,
        do_lower_case=args.do_lower_case,
    )
    model = model_type.model.from_pretrained(
        pretrained_model_name_or_path=args.pretrained_model_name_or_path,
    )

    if args.test_filename and not os.path.exists(args.test_filename):
        logger.error(
            f"Test filename provided ('{args.test_filename}') not found.")
        raise RuntimeError(
            f"Test filename provided ('{args.test_filename}') not found.")

    df = pd.read_csv(args.test_filename)
    logger.info(f"Loaded {len(df)} test examples.")
    logger.info(pformat(df.head()))

    # this should all work with loading saved models, right?

    # okay, now initialize the inference loop

    inference = pipeline(
        "text-classification",
        model=model,
        tokenizer=tokenizer,
        device=args.device,
        framework="pt",
        truncation=True
    )

    # extract label names
    label_names = sorted(df.event_type.unique().tolist())

    # create label2id
    label2id = {
        label: i
        for i, label in enumerate(label_names)
    }

    dataset = DoceeForInference(df, use_title=args.use_title)

    y_pred = np.array([
        int(out["label"].split("_")[1])
        for out in tqdm(inference(
            dataset,
            truncation=True,
            batch_size=args.batch_size,
            num_workers=args.num_workers
        ), desc=f"Inference, BS = {args.batch_size}", total=len(dataset))
    ])

    logger.info(f"Example predictions: {pformat(y_pred[:5])}")

    cls_metrics = classification_report(
        y_true=list(map(label2id.__getitem__, df.event_type.values)),
        y_pred=y_pred,
        target_names=label_names,
        output_dict=True
    )

    logger.info(pformat(cls_metrics))

    # save cls_metrics to a file
    with open(args.output_path, "w") as f:
        json.dump(cls_metrics, f, indent=2)

    logger.info(f"Results saved to {args.output_path}")


if __name__ == '__main__':
    main()
