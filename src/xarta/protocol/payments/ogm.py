r"""
Module for generating and validating the Belgian OGM.
"""

from __future__ import annotations

import random


def generate_ogm_code(reference: str | None = None) -> str:
    """
    Generates a Belgian structured remittance code (OGM).

    Parameters:
        reference (str): The customer number or invoice number (10 digits).

    Returns:
        str: The formatted OGM code, including checksum.
    """
    # Check if a reference has been specified.
    if not reference:
        # Generate a random 10 digit code.
        reference = "".join([str(random.randint(0, 9)) for _ in range(10)])

    # Ensure the reference is exactly 10 digits
    if len(reference) != 10 or not reference.isdigit():
        raise ValueError("The reference must be a 10-digit number.")

    # Step 1: Concatenate the first 10 digits
    concatenated_number = reference

    # Step 2: Calculate the checksum (modulo 97)
    checksum = int(concatenated_number) % 97
    if checksum == 0:
        checksum = 97

    # Step 3: Format the OGM code
    # Split into groups: 3 digits, 4 digits, 5 digits
    group_1 = concatenated_number[:3]
    group_2 = concatenated_number[3:7]
    group_3 = concatenated_number[7:]

    return f"+++{group_1}/{group_2}/{group_3}{checksum:02}+++"
