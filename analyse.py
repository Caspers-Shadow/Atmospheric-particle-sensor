#!/usr/bin/env python3

import pandas as pd

# ----------------------------------------------------
# LOAD DATA
# ----------------------------------------------------

try:
    df = pd.read_csv("thingspeak_data.csv")
except FileNotFoundError:
    print("ERROR: thingspeak_data.csv not found.")
    exit(1)

# Convert timestamps
df["created_at"] = pd.to_datetime(df["created_at"])

# Convert UTC to South African time
df["created_at"] = (
    df["created_at"]
    .dt.tz_convert("Africa/Johannesburg")
)

print("\n===== DATASET SUMMARY =====")
print(f"Total Readings: {len(df)}")

print("\nDate Range:")
print(f"Start: {df['created_at'].min()}")
print(f"End:   {df['created_at'].max()}")

print("\nAvailable data:")
print(f"From: {df['created_at'].min()}")
print(f"To:   {df['created_at'].max()}")

# ----------------------------------------------------
# RENAME COLUMNS
# ----------------------------------------------------

df.columns = [
    "created_at",
    "entry_id",
    "temperature",
    "humidity",
    "pressure",
    "pm1",
    "pm25",
    "pm10",
    "nh3_index",
    "light"
]

# ----------------------------------------------------
# PRINT STATISTICS
# ----------------------------------------------------

sensors = [
    "temperature",
    "humidity",
    "pressure",
    "pm1",
    "pm25",
    "pm10",
    "nh3_index",
    "light"
]

for sensor in sensors:

    print(f"\n===== {sensor.upper()} =====")

    print(
        df[sensor].describe()[
            ["count", "mean", "min", "max"]
        ]
    )

# ----------------------------------------------------
# SAVE CLEAN DATASET
# ----------------------------------------------------

df.to_csv(
    "cleaned_thingspeak_data.csv",
    index=False
)

print("\nSaved cleaned_thingspeak_data.csv")

# ----------------------------------------------------
# OPTIONAL: FILTER AN EXPERIMENT
# ----------------------------------------------------

answer = input(
    "\nWould you like to extract an experiment? (y/n): "
).lower()

if answer == "y":

    print(
        "\nExample format:\n"
        "2026-07-16 10:00:00"
    )

    start = input("Start time: ")
    end = input("End time: ")

    experiment = df[
        (df["created_at"] >= start)
        & (df["created_at"] <= end)
    ]

    filename = "experiment.csv"

    experiment.to_csv(
        filename,
        index=False
    )

    print(
        f"\nExported {len(experiment)} rows "
        f"to {filename}"
    )

print("\nAnalysis complete!")
