import json

def average(values):
    total = sum(values)
    return total / len(values)

def load(path):
    f = open(path)
    data = json.load(f)
    return data

def first_even(items):
    for i in range(len(items)):
        if items[i] % 2 == 0:
            return items[i]
