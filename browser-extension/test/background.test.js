'use strict'

import fetchMock from 'jest-fetch-mock'

import {fetchJson as fetchJson} from '../background.js'


fetchMock.enableMocks()

beforeEach(() => {
  fetch.resetMocks()
})

test('fetchJson', async () => {
  const data = { foo: { bar: 123 } }
  fetch.mockResponseOnce(JSON.stringify(data))
  expect(await fetchJson()).toStrictEqual(data)
})
