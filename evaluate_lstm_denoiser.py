from argparse import ArgumentParser

import pandas as pd

from dialogue_denoiser_lstm import make_dataset, load, evaluate


def configure_argument_parser():
    parser = ArgumentParser(description='Evaluate the LSTM dialogue filter')
    parser.add_argument('model_folder')
    parser.add_argument('dataset')

    return parser


def main(in_dataset, in_model_folder):
    lines_from, lines_to = in_dataset['utterance'], in_dataset['tags']
    data_points = [(tokens, tags) for tokens, tags in zip(lines_from, lines_to)] 
    model, vocab, char_vocab, label_vocab = load(in_model_folder)
    X, y = make_dataset(data_points, vocab, char_vocab, label_vocab)

    print 'Accuracy: {:.3f}'.format(evaluate(model, X, y))


if __name__ == '__main__':
    parser = configure_argument_parser()
    args = parser.parse_args()

    main(pd.read_json(args.dataset), args.model_folder)
