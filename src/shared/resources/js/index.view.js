// getParamString is used as a helper function to get parameters
// and works well with the +param mixin in the pug files.
function getParamString(paramName) {
  const paramId = `param_${paramName.toLowerCase()}`;
  const inputObject = document.getElementById(paramId);

  if (inputObject === null) {
    return null;
  }

  return inputObject.value;
}

function mapFormToJSON() {
  return {
    destination: {
      owner_id: '105b9291-6ff1-4938-899e-4bfafa69a6c7',
      id: getParamString('Destination'),
    },
    amount: {
      value: Number(getParamString('amount')),
      currency: getParamString('currency'),
    },
    description: getParamString('Description'),
    country_iso2: getParamString('Country_ISO'),
    auto_confirm: false,
    session_id: getParamString('DeviceSessionId'),
    funding_method: getParamString('Funding_Method'),
    initiated_at: new Date(),
    return_url: 'http://localhost:8080/payments/ok',
  };
}

function updateRequestJson() {
  const x = document.getElementById('req');
  x.innerText = JSON.stringify(mapFormToJSON(), null, 2);
}

function onInput() {
  updateRequestJson();
}

function load() {
  const urlSplit = window.location.href.split('?');

  if (urlSplit.length === 2) {
    const param = urlSplit[1].split('=');

    if (param[0] === 'return') {
      document.getElementById('error').classList.remove('is-error');
      document.getElementById('error').classList.add('is-success');
      document.getElementById('error-text').innerText = 'Thank you for topping up!';
      document.getElementById('error').classList.remove('hidden');
    }
  }

  document.onkeyup = onInput; // Prematurely update the json screen.

  updateRequestJson();
} // updateRequestJson is used to update the frontend json; to give the
// end-user a better grasp of what exactly they're sending.

function redirectPage(response) {
  if (response == null) {
    return null;
  }

  if (response.headers.has('location')) {
    window.location.replace(response.headers.get('location'));
  }
  return response;
}

function checkErrors(response) {
  if (response == null) {
    return null;
  }

  if (response.status === 400) {
    document.getElementById('error').classList.remove('hidden');
    document.getElementById('error-text').innerText = response.statusText;
  }
  return response;
}

export function closeError() {
  document.getElementById('error').classList.add('hidden');
}
export async function sendRequest() {
  const context = mapFormToJSON();
  let endpoint = getParamString('endpoint_url'); // Assume http if none are given.

  if (!endpoint.startsWith('http://')
    && !endpoint.startsWith('https://')) {
    endpoint = `http://${endpoint}`;
  }

  const body = JSON.stringify(context);

  fetch(`${endpoint}/init-deposit`, {
    method: 'POST',
    body,
    headers: {
      'Content-Type': 'application/json',
    },
  })
  // If a bad request is received; show it as a quality-of-life feature.
    .then(checkErrors)
  // When there's a 'Location' header present; redirect the browser.
    .then(redirectPage);
}

window.onload = load;
