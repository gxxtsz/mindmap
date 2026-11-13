"""内部持久化 AVL 映射：修改只复制搜索路径，几何版本之间共享未变化数据。"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _Entry:
    key: str
    value: object
    left: _Entry | None = None
    right: _Entry | None = None
    height: int = 1
    count: int = 1


def _height(node):
    return node.height if node else 0


def _entry(key, value, left=None, right=None):
    return _Entry(key, value, left, right, 1 + max(_height(left), _height(right)),
                  1 + (left.count if left else 0) + (right.count if right else 0))


def _left(node):
    top = node.right
    return _entry(top.key, top.value, _entry(node.key, node.value, node.left, top.left), top.right)


def _right(node):
    top = node.left
    return _entry(top.key, top.value, top.left, _entry(node.key, node.value, top.right, node.right))


def _balanced(key, value, left, right):
    if _height(left) - _height(right) > 1:
        if _height(left.left) < _height(left.right):
            left = _left(left)
        return _right(_entry(key, value, left, right))
    if _height(right) - _height(left) > 1:
        if _height(right.right) < _height(right.left):
            right = _right(right)
        return _left(_entry(key, value, left, right))
    return _entry(key, value, left, right)


def _set(node, key, value):
    if node is None:
        return _entry(key, value)
    if key == node.key:
        return _entry(key, value, node.left, node.right)
    if key < node.key:
        return _balanced(node.key, node.value, _set(node.left, key, value), node.right)
    return _balanced(node.key, node.value, node.left, _set(node.right, key, value))


def _delete(node, key):
    if node is None:
        raise KeyError(key)
    if key < node.key:
        return _balanced(node.key, node.value, _delete(node.left, key), node.right)
    if key > node.key:
        return _balanced(node.key, node.value, node.left, _delete(node.right, key))
    if node.left is None:
        return node.right
    if node.right is None:
        return node.left
    successor = node.right
    while successor.left is not None:
        successor = successor.left
    return _balanced(successor.key, successor.value, node.left, _delete(node.right, successor.key))


class PersistentMap(MutableMapping):
    """可写句柄包裹不可变树；fork 共享整棵树，后续修改只替换各自句柄的根。

    值必须视为不可变。几何使用元组、标量及不可变同级索引；嵌套字典在发布后
    不再原地修改。键采用稳定节点标识，查找、设置和删除均为 O(log N)。
    """

    def __init__(self, values=()):
        items = sorted(dict(values).items())

        def build(low, high):
            if low >= high:
                return None
            middle = (low + high) // 2
            key, value = items[middle]
            return _entry(key, value, build(low, middle), build(middle + 1, high))

        self._root = build(0, len(items))

    def fork(self):
        """常数成本取得独立句柄，用于冻结布局结果或执行可丢弃的几何试算。"""
        result = object.__new__(type(self))
        result._root = self._root
        return result

    def __len__(self):
        return self._root.count if self._root else 0

    def __getitem__(self, key):
        current = self._root
        while current is not None:
            if key == current.key:
                return current.value
            current = current.left if key < current.key else current.right
        raise KeyError(key)

    def __setitem__(self, key, value):
        self._root = _set(self._root, key, value)

    def __delitem__(self, key):
        self._root = _delete(self._root, key)

    def __iter__(self):
        pending, current = [], self._root
        while pending or current is not None:
            if current is not None:
                pending.append(current)
                current = current.left
            else:
                current = pending.pop()
                yield current.key
                current = current.right
