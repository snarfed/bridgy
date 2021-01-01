'use strict'

import {
  injectGlobals, login, INDIEAUTH_START,
} from '../common.js'

beforeAll(() => {
  injectGlobals({
    // browser is a namespace, so we can't use jest.mock(), have to mock and inject
    // it manually like this.
    browser: {
      tabs: {
        create: jest.fn(),
      },
      storage: {
        sync: {
          get: async () => browser.storage.sync.data,
          set: async values => Object.assign(browser.storage.sync.data, values),
          data: {},
        },
      },
    },
    console: {
      debug: () => null,
      log: () => null,
    },
    _console: console,
  })
})

beforeEach(() => {
  jest.resetAllMocks()
})

afterEach(() => {
  jest.restoreAllMocks()
})


test('login, no existing token', async () => {
  expect(browser.storage.sync.data).toEqual({})

  await login()

  const token = browser.storage.sync.data.token
  expect(token).toBeDefined()

  expect(browser.tabs.create.mock.calls.length).toEqual(1)
  expect(browser.tabs.create.mock.calls[0]).toEqual([{
    url: INDIEAUTH_START + '?token=' + token,
  }])
})

test('login, existing token', async () => {
  browser.storage.sync.data = {token: 'foo'}
  await login()
  expect(browser.tabs.create.mock.calls.length).toEqual(0)
})
