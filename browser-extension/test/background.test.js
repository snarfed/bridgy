'use strict'

import fetchMock from 'jest-fetch-mock'

import {forward, injectBrowser} from '../background.js'


fetchMock.enableMocks()

// browser is a namespace, so we can't use jest.mock(), have to mock and inject
// it manually like this.
let browser = {
  cookies: {
    getAll: jest.fn(),
  },
}

beforeAll(() => {
  injectBrowser(browser)
})

beforeEach(() => {
  fetch.resetMocks()
})

afterEach(() => {
  jest.restoreAllMocks()
})


test('forward', async () => {
  browser.cookies.getAll.mockResolvedValue([
    {name: 'foo', value: 'bar'},
    {name: 'baz', value: 'biff'},
  ])

  const body = 'some data'
  fetch.mockResponseOnce(body)

  await forward('/ig-path', '/br-path')
  expect(fetch.mock.calls.length).toBe(2)
  expect(fetch.mock.calls[0]).toEqual([
    'https://www.instagram.com/ig-path',
    {
      method: 'GET',
      credentials: 'same-origin',
      headers: {
        'Cookie': 'foo=bar; baz=biff',
        'User-Agent': navigator.userAgent,
      },
    },
  ])
  expect(fetch.mock.calls[1]).toEqual([
    'https://brid.gy/br-path',
    {
      method: 'POST',
      body: body,
    },
  ])
})
