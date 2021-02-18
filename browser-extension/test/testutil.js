import fetchMock from 'jest-fetch-mock'

fetchMock.enableMocks()

beforeAll(() => {
  Object.assign(global, {
    // browser is a namespace, so we can't use jest.mock(), have to mock and inject
    // it manually like this.
    browser: {
      cookies: {
        getAll: jest.fn(),
        getAllCookieStores: async () => [{id: '1'}],
      },
      storage: {
        sync: {
          get: async function () { return this.data },
          set: async function (values) { Object.assign(this.data, values) },
          data: {},
        },
        local: {
          get: async function () { return this.data },
          set: async function (values) { Object.assign(this.data, values) },
          data: {},
        },
      },
      tabs: {
        create: jest.fn(),
      },
      webRequest: {
        onBeforeSendHeaders: {
          hasListener: (fn) => false,
          addListener: (fn, filter, extra) => null,
        },
      },
    },
    console: {
      debug: () => null,
      log: () => null,
      warn: () => null,
      error: () => null,
    },
    _console: console,
  })
})

beforeEach(() => {
  jest.resetAllMocks()
  fetch.resetMocks()
  browser.storage.sync.data = {'token': 'towkin'}
})

afterEach(() => {
  jest.restoreAllMocks()
})
