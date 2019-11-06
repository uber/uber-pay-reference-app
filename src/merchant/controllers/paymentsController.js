const express = require('express');

class PaymentsController {
  constructor(uberClient) {
    this.uberClient = uberClient;
  }

  // Initialize the payment; requires a proper order to be processed.
  initPayment(req, res) {
    if (req.query.sessionId == null) {
      return res.render('403');
    }

    const x = res.cache.get(`session:${req.query.sessionId}`);
    if (x == null) {
      return res.render('403');
    }

    return res.render('payment_init', {
      session: req.query.sessionId,
      data: x.data,
      res: x.res,
    });
  }

  // Cancels the deposit from the user side.
  async cancelPayment(req, res) {
    const value = res.cache.get(`session:${req.params.id}`);
    if (value == null) {
      return res.render('403');
    }

    try {
      const x = await this.uberClient.cancelDepositAsync(
        value.res.deposit.id, { cancel_reason: 'USER_CANCELLED' },
      );
      return res.render('payment_cancel', {
        data: x.data,
      });
    } catch (ex) {
      return res.render(ex.response.status.toString());
    }
  }

  // Confirms the deposit from the user side.
  async confirmPayment(req, res) {
    // Get the user session data from the cache. This session data includes our current
    // deposit context.
    const value = res.cache.get(`session:${req.params.id}`);
    if (value == null) {
      // If the session does not exist, the user cannot confirm a deposit, therefore
      // is not allowed to be here.
      return res.render('403');
    }

    // Confirm the deposit that is in context. This gives Uber a signal that we have
    // received starts the movement of money on Uber's side.
    try {
      const x = await this.uberClient.confirmDepositAsync(value.res.deposit.id);
      // Show a nice page back.
      return res.render('payment_ok', {
        data: x.data,
      });
    } catch (ex) {
      return res.render(ex.response.status.toString());
    }
  }

  getRouter() {
    return express.Router()
      .get('/init', (req, res) => this.initPayment(req, res))
      .get('/:id/cancel', (req, res) => this.cancelPayment(req, res))
      .get('/:id/confirm', (req, res) => this.confirmPayment(req, res));
  }
}

module.exports = PaymentsController;
