def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


print("=== 간단 계산기 ===")

a = int(input("첫 번째 숫자: "))
b = int(input("두 번째 숫자: "))

print("더하기:", add(a, b))
print("빼기:", subtract(a, b))