'use strict'

import {
  BRIDGY_BASE_URL,
} from '../common.js'

import {
  Facebook,
} from '../facebook.js'

import './testutil.js'

beforeEach(() => {
  browser.storage.local.data = {'facebook-bridgySourceKey': 'KEE'}
  browser.cookies.getAll.mockResolvedValue([{name: 'xs', value: 'foo'}])
})

const activities = [{
  id: '246',
  fb_id: '222',
  url: 'https://mbasic.facebook.com/246',
  object: {
    replies: {totalItems: 3},
    fb_reaction_count: 5,
  },
}, {
  id: '357',
  fb_id: '333',
  url: 'https://mbasic.facebook.com/357',
  object: {
    replies: {totalItems: 0},
    fb_reaction_count: 0,
  },
}]


test('poll', async () => {
  fetch.mockResponseOnce('fb feed')
  fetch.mockResponseOnce(JSON.stringify(activities))
  fetch.mockResponseOnce('post 246')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('likes 246')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('post 357')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('likes 357')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('"OK"')

  await new Facebook().poll()
  expect(fetch.mock.calls.length).toBe(11)

  expect(await browser.storage.local.get()).toMatchObject({
    'facebook-post-246': {c: 3, r: 5},
    'facebook-post-357': {c: 0, r: 0},
  })

  expect(fetch.mock.calls[0][0]).toBe('https://mbasic.facebook.com/me')
  expect(fetch.mock.calls[1][0]).toBe(
    `${BRIDGY_BASE_URL}/facebook/browser/feed?token=towkin&key=KEE`)
  expect(fetch.mock.calls[1][1].body).toBe('fb feed')

  for (const [i, fb_id, id] of [[2, '222', '246'], [6, '333', '357']]) {
    expect(fetch.mock.calls[i][0]).toBe(`https://mbasic.facebook.com/${id}`)
    expect(fetch.mock.calls[i + 1][0]).toBe(
      `${BRIDGY_BASE_URL}/facebook/browser/post?token=towkin&key=KEE`)
    expect(fetch.mock.calls[i + 1][1].body).toBe(`post ${id}`)
    expect(fetch.mock.calls[i + 2][0]).toContain('https://mbasic.facebook.com/ufi/reaction')
    expect(fetch.mock.calls[i + 2][0]).toContain(fb_id)
    expect(fetch.mock.calls[i + 3][0]).toBe(
      `${BRIDGY_BASE_URL}/facebook/browser/reactions?id=${id}&token=towkin&key=KEE`)
    expect(fetch.mock.calls[i + 3][1].body).toBe(`likes ${id}`)
  }

  expect(fetch.mock.calls[10][0]).toBe(
    `${BRIDGY_BASE_URL}/facebook/browser/poll?token=towkin&key=KEE`)
})
