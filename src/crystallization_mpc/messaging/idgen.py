import itertools


_seq = itertools.count(1)


def next_seq() -> int:
    return next(_seq)
