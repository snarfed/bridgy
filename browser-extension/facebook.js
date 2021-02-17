// TODO: update the profile on every poll, since the profile picture URL has a
// timestamp that expires.
'use strict'

import {Silo} from './common.js'


class Facebook extends Silo {
  /** See below class declaration for class static properties. */

  /**
   * Returns the URL path to the user's profile.
   */
  static async profilePath() {
    return '/profile.php?v=info'
  }

  /**
   * Returns the URL path to the user's feed of posts.
   */
  static async feedPath() {
    return '/me'
  }

  /**
   * Returns an AS activity's reaction count, if available.
   */
  static reactionsCount(activity) {
    return activity.object.fb_reaction_count
  }

  /**
   * Returns the URL path for a given activity's reactions.
   */
  static reactionsPath(activity) {
      return `/ufi/reaction/profile/browser/?ft_ent_identifier=${activity.fb_id}`
  }

  /**
   * Wrap and substitute mbasic for www.
   */
  static async siloGet(url) {
    return await super.siloGet(url.replace(this.NON_SCRAPED_BASE_URL, this.BASE_URL))
  }
}

Facebook.DOMAIN = 'facebook.com'
Facebook.NAME = 'facebook'
Facebook.BASE_URL = 'https://mbasic.facebook.com'
Facebook.NON_SCRAPED_BASE_URL = 'https://www.facebook.com'
Facebook.LOGIN_URL = `${Facebook.BASE_URL}/login`
Facebook.COOKIE = 'xs'

export {Facebook}
