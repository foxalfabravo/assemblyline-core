from math import tanh

COMPLETE_QUEUE_NAME = 'm-complete'
INGEST_QUEUE_NAME = 'm-ingest'


def drop_chance(length, maximum):
    return tanh(float(length - maximum) / maximum * 2.0)
