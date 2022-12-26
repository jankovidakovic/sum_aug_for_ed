import argparse
import logging
from pprint import pformat
import os

import pandas as pd
from tqdm import tqdm
from transformers import AutoTokenizer, pipeline, set_seed, enable_full_determinism
from transformers.utils import PaddingStrategy

from src.data import DoceeForInference
from src.utils import alternating_concat

logger = logging.getLogger(__name__)


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser("CLI args")
    parser.add_argument(
        "--pretrained_model_name_or_path",
        type=str,
        required=True,
        help="Generative model which will be used for summarization"
    )
    parser.add_argument(
        "--random_seed",
        type=int,
        default=42,
        help="Random seed which controls randomness during text generation (if any). Defaults to 42"
    )
    parser.add_argument(
        "--input_path",
        type=str,
        required=True,
        help="Path to a csv file containing the dataset to be augmented."
    )
    parser.add_argument(
        "--output_path",
        type=str,
        required=True,
        help="Path to which the augmented dataset will be saved."
    )
    parser.add_argument(
        "--device",
        type=int,
        required=True,
        help="Device to use during inference. -1 defaults to CPU, while >=0 specifies a GPU number."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="If set, logging level will be set to info."
    )
    parser.add_argument(
        "--min_length",
        type=int,
        default=20,
        help="Minimum summarization length. Defaults to 20."
    )
    parser.add_argument(
        "--max_length",
        type=int,
        default=100,
        help="Maximum summarization length. Defaults to 100."
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        required=True,
        help="Batch size to use during inference"
    )
    parser.add_argument(
        "--num_workers",
        type=int,
        required=False,
        default=4,
        help="Number of workers to use for loading the data. Defaults to 4."
    )
    parser.add_argument(
        "--low_resource_cutoff",
        type=int,
        required=False,
        default=None,
        help="If provided, will only augment examples from classes which have no more examples than the "
             "low resource cutoff."
    )
    parser.add_argument(
        "--use_title",
        action="store_true",
        help="If provided, will include the title (along with text) for summarization"
    )
    parser.add_argument(
            "--do_sample",
            action="store_true",
            help="If this option is set, then at the each time step of the generation, "
                "next token will be sampled from the probability distribution over the "
                "vocabulary. "
            )

    # argument groups
    parser.add_argument(
        "--early_stopping",
        action="store_true",
        help="If this option is provided, text generation will finish as soon"
            " as the EOS token has been generated by all beams."
    )
    parser.add_argument(
        "--num_beams",
        type=int,
        default=4,
        help="Number of beams to use for beam search. Defaults to 4."
             "Setting this value to 1 means no beam search."
    )

    parser.add_argument(
        "--top_k",
        type=int,
        required=False,
        default=0,
        help="In each time step, only the top k most probable words are considered. "
        "Defaults to 0, which means that top-k decoding is turned off."
    )
    parser.add_argument(
        "--top_p",
        type=float,
        required=False,
        default=1.0,
        help="In each time step, the set of words to sample from is chosen "
        "such that the set's probability mass minimally exceeds the given threshold. "
        "defaults to 1.0, which means that top-p decoding is turned off."
    )

    parser.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="Controls the sharpness of the probability distribution generated by softmax. "
        "softmax(x; T) = exp(x/T) / sum_i exp(x_i / T). "
        "Defaults to 1.0. Setting T > 1 will make the distribution smoother. "
        "Setting T < 1 will make the distribution sharper."
    )

    parser.add_argument(
        "--num_return_sequences",
        type=int,
        default=1,
        help="Number of sequences to return for each source document which is summarized. "
        "Note that this option doesn't make sense unless `do_sample` is also set. Defaults to 1."
        )

    parser.add_argument(
        "--penalty_alpha",
        type=float,
        default=0,
        help="Alpha for contrastive search. Defaults to 0 (turned off)"
    )


    return parser


def main():
    args = get_parser().parse_args()
    logging.root.handlers = []
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        # log info only on main process
        level=logging.INFO if args.verbose else logging.WARNING,
        handlers=[
            logging.StreamHandler(),
        ],
    )

    set_seed(args.random_seed)
    enable_full_determinism(args.random_seed)
    logger.info(f"Random seed is set to: {args.random_seed}")

    df = pd.read_csv(args.input_path)
    logger.info(f"Loaded {len(df)} examples from {os.path.abspath(args.input_path)}")

    # df has "id" column

    tokenizer = AutoTokenizer.from_pretrained(
        args.pretrained_model_name_or_path,
        padding=PaddingStrategy.MAX_LENGTH,
        use_fast=True
    )
    summarizer = pipeline(
        "summarization",
        model=args.pretrained_model_name_or_path,
        tokenizer=tokenizer,
        device=args.device,
        framework="pt"
    )

    # retain only columns relevant for event classification
    df = df.loc[:, ["text", "title", "event_type", "date"]]
    logger.info(f"Retaining only columns: {pformat(df.columns)}")
    summary_df = df.copy()
    # TODO - make relevant columns CLI-supplied

    if args.low_resource_cutoff:
        logger.info(f"Low resource cutoff set to {args.low_resource_cutoff}."
                    f"Augmenting only classes with no more than "
                    f"{args.low_resource_cutoff} examples.")
        # filter based on class count
        from src.utils import low_resource_slice
        low_resource_classes, summary_df = low_resource_slice(
            summary_df,
            args.low_resource_cutoff,
            return_classes=True
        )
        logger.info(f"Low resource classes: {pformat(low_resource_classes)}")

    # after low-resource slice, ids are still retained

    # save the correct indices
    source_doc_ids = summary_df.index.values  # np.array

    total_summaries = len(summary_df) * args.num_return_sequences
    logger.info(f"For each of the {len(summary_df)} source documents, "
               f"{args.num_return_sequences} will be generated. In total, "
               f"{total_summaries} summaries will be generated."
    )

    dataset = DoceeForInference(summary_df, use_title=args.use_title)

    if args.num_beams > 1:
        logger.info(f"Using beam search with {args.num_beams} beams.")
        if not args.early_stopping:
            logger.warning(f"Beam search is used, but early_stopping is not set. "
                           "You might want to set early stopping.")

    if args.do_sample:
        logger.info(
            "Text is generated using sampling. "
            f"Temperature is set to {args.temperature}"
        )

    if args.top_k > 0:
        logger.info(f"Using top-k sampling with k = {args.top_k}")

    if args.top_p != 1.0:
        logger.info(f"Using top-p sampling with p = {args.top_p}")

    if args.penalty_alpha != 0:
        logger.info(f"Using contrastive search with alpha = {args.penalty_alpha}")

    # make space for all the examples
    summary_df = alternating_concat(
            summary_df.loc[:, ["title", "date", "event_type"]], 
            args.num_return_sequences
    )

    assert len(summary_df) == total_summaries, f"Length of summary_df should be"
    f" equal to {total_summaries}, but is equal to {len(summary_df)} instead."

    summary_df.loc[:, ["text", "source_doc_id"]] = [
        (out[j]["summary_text"], source_doc_ids[i])
        for i, out in enumerate(tqdm(summarizer(
            dataset,
            truncation=True,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            min_length=args.min_length,
            max_length=args.max_length,
            num_beams=args.num_beams,
            early_stopping=args.early_stopping,
            top_k=args.top_k,
            top_p=args.top_p,
            temperature=args.temperature,
            do_sample=args.do_sample,
            num_return_sequences=args.num_return_sequences,
            penalty_alpha=args.penalty_alpha
        ), desc=f"Inference loop", total=len(dataset)))
        for j in range(len(out))

        # early_stopping :: bool
        #   if set to True, then generation will finish as soon as all beams generate EOS token
        #   tbh it doesnt make sense to ever not set this to true
        #
        # no_repeat_ngram_size :: int
        #   ngrams of that size will not be repeated during text generation
        #   careful about that -> e.g. if "New York" is present in the article,
        #   and we set no_repeat_ngram_size to 2, then "New York" will never repeat
        #
        # num_return_sequences :: int
        #   set this to return multiple sequences at once. TODO : check how does this impact memory usage
        #
        # do_sample :: bool
        #   if set to true, w_t is sampled from P(w | w_{1:(t-1)}). In other words, text
        #   generation is no longer deterministic
        #   -> generation depends on the random seed. TODO : implement a function for random seeding
        #
        # temperature :: float
        #   temperature controls the sharpness of the probability distribution generated by softmax
        #   softmax(x;T) = exp(x/T) / sum_i exp(x_i / T)
        #   T -> 0 -- distribution decays into a dirac impulse at the most probable point (equal to greedy decoding)
        #   T -> inf -- distribution decays into an uniform distribution
        #   T < 1 -> distribution is sharper  (bias towards tokens with higher probability)
        #   T > 1 -> distribution is smoother
        #
        # top_k :: int
        #   if set to >0, applies top-k sampling. In each time step, only the top k most probable tokens
        #   are considered, and probability mass is redistributed amongst those tokens before sampling
        #   -> the problem with top_k is that it doesn't dynamically adapt the number of words to sample
        #
        # top_p :: float
        #   if set to >0, applies top-p (nucleus) sampling. A mimimum size set of most probable tokens
        #   is chosen, such that the probability mass of the set exceeds the top_p parameter.
        #   this is good because the size of the set is dynamic and depends on the concrete distribution
        #   of next token
        #
        # penalty_alpha -> alpha for contrastive search.
        #   contrastive search is supposed to be SOTA, but after reading the paper, it seems that:
        #       contrastive search only works better than top-p when used with the model
        #       which was trained with the addition of contrastive loss
        #     -> we can resolve this issue by training BART on CNN/DM using contrastive loss,
        #           but that frankly seems like an overkill
        # TODO - implement a CLI interface for different generating strategy:
        #   beam_search
        #   greedy
        #   top-k
        #   top-p
        #   contrastive_search
        #
        # TODO - implement generation of multiple sequences at once
        # TODO - figure out what is the best way to save the generated summaries
        # TODO - add source document ID for each summary

    ]

    assert len(summary_df) == total_summaries, f"Length of summary_df should be"
    f" equal to {total_summaries}, but is equal to {len(summary_df)} instead."

    # set the source document ids
    logger.info(f"Summary_df preview: {pformat(summary_df.head())}")
    df_to_save = pd.concat((df, summary_df), ignore_index=True)

    logger.info(f"Columns of concatenated dataset: {df_to_save.columns}")

    # since unsummarized examples come first, their ids will correctly
    #   be set in accordance to source_doc_id
    # unsummarized examples will have source_doc_id set to NaN

    logging.info(f"Length of concatenated dataset: {len(df_to_save)}")
    logging.info(pformat(df_to_save.head()))
    logging.info(f"Columns: {df_to_save.columns}")

    # TODO - save hyperparameter info, or do dataset versioning via W&B

    df_to_save.to_csv(args.output_path, index_label="id")
    logger.info(f"Successfully saved resulting dataset at path  {os.path.abspath(args.output_path)}")


if __name__ == '__main__':
    main()
