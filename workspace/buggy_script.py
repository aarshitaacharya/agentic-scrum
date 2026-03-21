# workspace/buggy_script.py
# This is the toy file our agents will work on.
# It has two deliberate bugs for the PM to find.

def get_average(numbers):
    # BUG 1: divides by hardcoded 10 instead of len(numbers)
    total = sum(numbers)
    return total / 10

def find_max(numbers):
    # BUG 2: starts max_val too high, will never update for normal lists
    max_val = 99999
    for n in numbers:
        if n > max_val:
            max_val = n
    return max_val

def greet_user(name):
    # This one is fine
    return f"Hello, {name}!"


if __name__ == "__main__":
    nums = [4, 7, 2, 9, 1]
    print("Average:", get_average(nums))   # should be 4.6, will print 2.3
    print("Max:", find_max(nums))          # should be 9, will print 99999
    print(greet_user("Ada"))