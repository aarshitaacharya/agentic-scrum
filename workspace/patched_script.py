def flatten(nested_list, depth=1):
    result = []
    for item in nested_list:
        if isinstance(item, list) and depth > 0:
            result.extend(flatten(item, depth - 1))
        else:
            result.append(item)
    return result

def running_total(transactions):
    total = transactions[0]
    for amount in transactions[1:]:
        total += amount
    return total

def chunk_list(lst, size):
    chunks = []
    for i in range(0, len(lst), size):
        chunks.append(lst[i:i + size])
    return chunks


if __name__ == "__main__":
    print(flatten([[1, [2, 3]], [4, [5]]], depth=1))
    print(running_total([10, 20, 30, 40]))
    print(chunk_list([1,2,3,4,5,6], 2))