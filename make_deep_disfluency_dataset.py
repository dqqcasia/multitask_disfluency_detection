import sys
import os
from argparse import ArgumentParser

import pandas as pd

THIS_FILE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(THIS_FILE_DIR,
                        'deep_disfluency',
                        'deep_disfluency',
                        'data',
                        'disfluency_detection',
                        'switchboard')

sys.path.append(os.path.join(THIS_FILE_DIR, 'deep_disfluency'))

from deep_disfluency.feature_extraction.feature_utils import (load_data_from_disfluency_corpus_file,
                                                              convert_from_inc_disfluency_tags_to_eval_tags)


def deep_disfluency_dataset_to_data_frame(in_dataset):
    eval_tags = [convert_from_inc_disfluency_tags_to_eval_tags(tags, tokens, representation='disf1')
                 for tags, tokens in zip(in_dataset[4], in_dataset[2])]
    return pd.DataFrame({'utterance': in_dataset[2],
                         'pos': in_dataset[3],
                         'tags': in_dataset[4],
                         'eval_tags': eval_tags})


def get_unique_elements(in_list):
    result = set([])
    for sequence in in_list:
        result.update(sequence)
    return result


def main(in_result_folder):
    train = load_data_from_disfluency_corpus_file(os.path.join(DATA_DIR, 'swbd_disf_train_1_data.csv'),
                                                  representation='disf1',
                                                  limit=8,
                                                  convert_to_dnn_format=True)
    dev = load_data_from_disfluency_corpus_file(os.path.join(DATA_DIR, 'swbd_disf_heldout_data.csv'),
                                                representation='disf1',
                                                limit=8,
                                                convert_to_dnn_format=True)
    test = load_data_from_disfluency_corpus_file(os.path.join(DATA_DIR, 'swbd_disf_test_data.csv'),
                                                 representation='disf1',
                                                 limit=8,
                                                 convert_to_dnn_format=True)
    print 'Trainset size: {} utterances'.format(len(train[0]))
    print 'Devset size: {} utterances'.format(len(dev[0]))
    print 'Testset size: {} utterances'.format(len(test[0]))
    unique_train_tags = get_unique_elements(train[4])
    print 'Unique #tags in trainset: {}'.format(len(unique_train_tags))

    if not os.path.exists(in_result_folder):
        os.makedirs(in_result_folder)
    (deep_disfluency_dataset_to_data_frame(train)).to_json(os.path.join(in_result_folder, 'trainset.json'))
    (deep_disfluency_dataset_to_data_frame(dev)).to_json(os.path.join(in_result_folder, 'devset.json'))
    (deep_disfluency_dataset_to_data_frame(test)).to_json(os.path.join(in_result_folder, 'testset.json'))


def configure_argument_parser():
    parser = ArgumentParser(description='Make dataset from deep_disfluency (Hough, Schlangen 2015)')
    parser.add_argument('result_folder')

    return parser


if __name__ == '__main__':
    parser = configure_argument_parser()
    args = parser.parse_args()

    main(args.result_folder)
