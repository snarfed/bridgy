'use strict'

import {login} from '../common.js'

document.addEventListener('DOMContentLoaded', function () {
  document.querySelector('#reconnect').addEventListener('click', () => login(true))
})
