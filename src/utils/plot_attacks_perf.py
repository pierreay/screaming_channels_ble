#!/usr/bin/env python3

"""Read the output CSV file from the Bash partner script and plot the results."""

import numpy as np
import matplotlib.pyplot as plt
import csv

# * Configuration

# CSV file name.
FILE="attack_results.csv"

# Number of columns inside the CSV file.
NCOL=0

# * CSV reader

print("Open {}...".format(FILE))

# Grep number of columns in CSV file.
if NCOL == 0:
    with open(FILE, 'r') as csvfile:
        line = csvfile.readline()
        nsep = line.count(';')
        NCOL = nsep + 1 if line[:-1] != ';' else nsep

print("NCOL={}".format(NCOL))

# X-axis, number of traces.
x_nb = []
# Y-avis, PGE median.
y_pge = []
# Y-axis, log_2(key rank).
y_kr = []

# Read the CSV file into lists.
with open(FILE, 'r') as csvfile:
    rows = csv.reader(csvfile, delimiter=';')
    # Iterate over lines.
    for i, row in enumerate(rows):
        # Skip header.
        if i == 0:
            continue
        # Get data. Index is the column number. Do not index higher than NCOL.
        x_nb.append(int(float(row[0])))
        y_kr.append(int(float(row[1])))
        y_pge.append(int(float(row[3])))

print("x_nb={}".format(x_nb))
print("y_kr={}".format(y_kr))
print("y_pge={}".format(y_pge))

# * Key rank plot

plt.plot(x_nb, y_kr, "-*")
plt.xlabel('Number of traces')
plt.ylabel('Log2(Key rank)')
plt.title('Key rank vs. Trace number')
plt.show()

# * PGE plot

plt.plot(x_nb, y_pge, "-*")
plt.xlabel('Number of traces')
plt.ylabel('Median(PGE)')
plt.title('PGE vs. Trace number')
plt.show()
