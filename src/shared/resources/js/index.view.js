window.onload = function () {
  let urlSplit = window.location.href.split('?');

  if (urlSplit.length == 2) {
    let param = urlSplit[1].split('=');

    if (param[0] == 'return') {
      document.getElementById('error').classList.remove('is-error');
      document.getElementById('error').classList.add('is-success');
      document.getElementById('error-text').innerText = "Thank you for topping up!";
      document.getElementById('error').classList.remove('hidden');
    }
  }

  document.onkeyup = function (x) {
    updateRequestJson();
  }; // Prematurely update the json screen.


  updateRequestJson();
}; // updateRequestJson is used to update the frontend json; to give the 
// end-user a better grasp of what exactly they're sending.


function updateRequestJson() {
  let x = document.getElementById('req');
  x.innerText = JSON.stringify(mapFormToJSON(), null, 2);
}

export async function sendRequest() {
  let context = mapFormToJSON();
  let endpoint = getParamString("endpoint_url"); // Assume http if none are given.

  if (!endpoint.startsWith("http://") && !endpoint.startsWith("https://")) {
    endpoint = "http://" + endpoint;
  }

  const body = JSON.stringify(context);

  fetch(endpoint + "/init-deposit", {
    method: 'POST',
    body: body,
    headers: {
      "Content-Type": "application/json"
    }
  })
  // If a bad request is received; show it as a quality-of-life feature.
    .then(checkErrors)
  // When there's a 'Location' header present; redirect the browser.
    .then(redirectPage)
}

function redirectPage(response) {
  if(response == null) {
    console.error("response is null.");
    return null;
  }

  console.log("trying to redirect...");
  if (response.headers.has('location')) {
    window.location.replace(response.headers.get('location'));
  }
  return response;
}

function checkErrors(response) {
  if(response == null) {
    console.error("response is null.");
    return null;
  }

  console.log("checking for errors...");
  if (response.status == 400) {
    document.getElementById('error').classList.remove('hidden');
    document.getElementById('error-text').innerText = x.statusText;
  }
  return response;
}

export function closeError() {
  document.getElementById('error').classList.add('hidden');
}

function mapFormToJSON() {
  return {
    destination: {
      owner_id: "105b9291-6ff1-4938-899e-4bfafa69a6c7",
      id: getParamString("Destination")
    },
    amount: {
      value: Number(getParamString('amount')),
      currency: getParamString("currency")
    },
    description: getParamString("Description"),
    country_iso2: getParamString("Country_ISO"),
    auto_confirm: false,
    session_id: getParamString('DeviceSessionId'),
    funding_method: getParamString("Funding_Method"),
    initiated_at: new Date(),
    return_url: "http://localhost/payments/ok"
  };
} 

// getParamString is used as a helper function to get parameters
// and works well with the +param mixin in the pug files.
function getParamString(paramName) {
  let paramId = "param_" + paramName.toLowerCase();
  let inputObject = document.getElementById(paramId);

  if (inputObject === null) {
    return null;
  }

  return inputObject.value;
}