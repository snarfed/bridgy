#!/usr/bin/python
"""Unit tests for util.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import unittest

import testutil
from util import KeyNameModel, Handler

from google.appengine.ext import db


class KeyNameModelTest(testutil.TestbedTest):

  def test_constructor(self):
    # with key name is ok
    entity = KeyNameModel(key_name='x')
    entity.save()
    db.get(entity.key())

    # without key name is not ok
    self.assertRaises(AssertionError, KeyNameModel)


class HandlerTest(testutil.HandlerTest):

  def _test_redirect(self, uri, messages, expected_location):
    self.handler.messages = messages
    self.handler.redirect(uri)
    self.assertEqual(302, self.response.status)
    self.assertEqual('http://HOST' + expected_location,
                     self.response.headers['Location'])

  def test_redirect(self):
    self._test_redirect('/', [], '/')

  def test_redirect_with_messages(self):
    self._test_redirect('/', ['foo', 'bar'], '/?msg=foo&msg=bar')

  def test_redirect_with_query_params_and_messages(self):
    self._test_redirect('/?x=y', [], '/?x=y')

  def test_redirect_with_query_params_and_messages(self):
    self._test_redirect('/?x=y&msg=baz', ['foo', 'bar'],
                        '/?x=y&msg=baz&msg=foo&msg=bar')
