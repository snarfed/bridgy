'use strict'

import {Silo} from './common.js'


class Instagram extends Silo {
  DOMAIN = 'instagram.com'
  NAME = 'instagram'
  BASE_URL = 'https://www.instagram.com'
  LOGIN_URL = `${this.BASE_URL}/accounts/login/`
  COOKIE = 'sessionid'

  /**
   * Returns the URL path to the user's profile, eg '/snarfed/'.
   */
  async profilePath() {
    let username = await this.storageGet('username')

    if (!username) {
      // extract username from a logged in home page fetch
      username = await this.forward('/', '/homepage')
      if (!username) {
        return
      }
      await this.storageSet('username', username)
    }

    return `/${username}/`
  }

  /**
   * Returns the URL path to the user's feed of posts.
   */
  feedPath = this.profilePath

  /**
   * Returns an AS activity's like count, if available.
   */
  reactionsCount(activity) {
    return activity.object.ig_like_count
  }

  /**
   * Returns the URL path for a given activity's likes.
   */
  reactionsPath(activity) {
    return `/graphql/query/?query_hash=d5d763b1e2acf209d62d22d184488e57&variables={"shortcode":"${activity.object.ig_shortcode}","include_reel":false,"first":100}`
  }
}

export {
  Instagram,
}
