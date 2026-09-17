# A small grade book with three real logic bugs in it.
# This is the default file the agents work on if you do not drop your own.

def average(scores):
    # BUG: off-by-one — the last score is never counted.
    total = 0
    for i in range(len(scores) - 1):
        total += scores[i]
    return total / len(scores)


def highest(scores):
    # BUG: starts at 0, so an all-negative list returns 0 instead of the max.
    best = 0
    for score in scores:
        if score > best:
            best = score
    return best


def letter_grade(score):
    # BUG: boundaries are wrong — 90 should be an A, 80 a B.
    if score > 90:
        return "A"
    elif score > 80:
        return "B"
    elif score >= 70:
        return "C"
    return "F"


def main():
    scores = [90, 80, 70, 100]
    print("scores:", scores)
    print("average:", average(scores))        # expected 85.0
    print("highest:", highest(scores))        # expected 100
    print("grade for 90:", letter_grade(90))  # expected A
    print("grade for 80:", letter_grade(80))  # expected B


if __name__ == "__main__":
    main()
