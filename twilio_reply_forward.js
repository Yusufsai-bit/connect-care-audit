/**
 * Twilio Function — Forward inbound WhatsApp replies to the manager's mobile.
 *
 * Deploy this in the Twilio Console:
 *   1. Go to: console.twilio.com → Functions → Services → Create Service "connect-care"
 *   2. Add Function → path: /forward-reply → paste this code → Save
 *   3. Deploy All
 *   4. Copy the Function URL (looks like: https://connect-care-XXXX.twil.io/forward-reply)
 *   5. Go to: Messaging → Senders → WhatsApp Senders → your number (+19794066545)
 *      → set "A message comes in" webhook to the URL above
 *   6. For the sandbox: Messaging → Try it out → Send a WhatsApp message
 *      → "When a message comes in" → paste the URL
 *
 * Environment variable to set in the Function (Configuration → Environment Variables):
 *   MANAGER_NUMBER  =  +61481140097
 */

exports.handler = function (context, event, callback) {
  const from   = event.From  || "Unknown";
  const body   = event.Body  || "(no message)";
  const client = context.getTwilioClient();

  // Strip the "whatsapp:" prefix for display
  const displayFrom = from.replace("whatsapp:", "");

  const forwardText =
    `Worker reply from ${displayFrom}:\n\n${body}\n\n` +
    `(Reply via dashboard to respond)`;

  client.messages
    .create({
      from: context.TWILIO_PHONE_NUMBER,   // your Twilio number +19794066545
      to:   context.MANAGER_NUMBER,         // +61481140097
      body: forwardText,
    })
    .then(() => {
      // Send a brief auto-reply back to the worker so they know it went through
      const twiml = new Twilio.twiml.MessagingResponse();
      twiml.message("Got it, thanks! Your message has been passed on.");
      callback(null, twiml);
    })
    .catch((err) => {
      console.error("Forward failed:", err);
      // Still send auto-reply even if forward fails
      const twiml = new Twilio.twiml.MessagingResponse();
      twiml.message("Got it, thanks!");
      callback(null, twiml);
    });
};
