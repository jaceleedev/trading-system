"""A fixed arithmetic context makes research results independent of a caller's globals."""

from decimal import ROUND_HALF_EVEN, Context, localcontext
from functools import wraps


def research_arithmetic(function):
    @wraps(function)
    def calculate(*args, **kwargs):
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            return function(*args, **kwargs)

    return calculate
