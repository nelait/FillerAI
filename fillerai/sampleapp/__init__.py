"""Northwind Mutual, the sample application that uses the chat service.

It is kept inside the package so ``fillerai serve`` can start it, but it is
written as an outside application: standard library only, nothing imported
from the rest of FillerAI, and every question it has goes over HTTP to
``/v1``. See ``app.py``.
"""
