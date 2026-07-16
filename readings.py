#!/usr/bin/env python3

import pandas as pd
import os

# Read the ThingSpeak CSV file
try:
    df = pd.read_csv("thingspeak_data.csv")
except FileNotFoundError:
    print("ERROR: Could not find 'thingspeak_data.csv'")
    exit(1)

# Display some information about the dataset
print("\n=== Dataset Information ===")
print(f"Total entries: {len(df)}")
print(f"Columns: {list(df.columns)}")

# Check that field8 (Experiment ID) exists
if "field8" not in df.columns:
    print(
        "\nERROR: 'field8' was not found.\n"
        "Make sure Field 8 in ThingSpeak is set to 'Experiment ID'."
    )
    exit(1)

# Convert Experiment ID values to integers where possible
df["field8"] = pd.to_numeric(df["field8"], errors="coerce")

# Get all unique experiment IDs
experiment_ids = sorted(df["field8"].dropna().unique())

print("\nExperiments found:")
for exp_id in experiment_ids:
    count = len(df[df["field8"] == exp_id])
    print(f"  Experiment {int(exp_id)}: {count} entries")

# Create an output directory if it doesn't exist
output_dir = "experiments"
os.makedirs(output_dir, exist_ok=True)

# Export each experiment to its own CSV file
for exp_id in experiment_ids:
    experiment_data = df[df["field8"] == exp_id]

    filename = os.path.join(
        output_dir,
        f"experiment_{int(exp_id)}.csv"
    )

    experiment_data.to_csv(
        filename,
        index=False
    )

    print(
        f"Exported Experiment {int(exp_id)} "
        f"({len(experiment_data)} entries) "
        f"-> {filename}"
    )

print("\nAll experiments exported successfully!")
