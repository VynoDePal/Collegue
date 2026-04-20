// Small JS module with 2 exports and 1 dead helper.

function formatName(user) {
  return `${user.firstName} ${user.lastName}`.trim();
}

function computeDiscount(price, percent) {
  return price * (1 - percent / 100);
}

function _unused_helper() {
  // Never referenced from outside.
  return "dead";
}

module.exports = { formatName, computeDiscount };
