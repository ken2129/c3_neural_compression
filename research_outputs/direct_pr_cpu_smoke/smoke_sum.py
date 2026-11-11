"""CPU-only sum smoke calculation using only the Python standard library."""
import json


def calculate(n):
    """Return an explicit loop sum and formula sum for a nonnegative integer."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError("n must be an integer")
    if n < 0:
        raise ValueError("n must be nonnegative")
    total = 0
    for value in range(1, n + 1):
        total += value
    formula = n * (n + 1) // 2
    return {"N": n, "loop_sum": total, "formula_sum": formula,
            "match": total == formula}


if __name__ == "__main__":
    print(json.dumps(calculate(1000), sort_keys=True))
