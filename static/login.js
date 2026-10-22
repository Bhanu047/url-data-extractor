'use strict';
document.getElementById('login').addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.getElementById('sign-in'); button.disabled = true;
  try {
    const response = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({code:document.getElementById('code').value})});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'Could not sign in.');
    window.location.assign('/');
  } catch (error) { document.getElementById('login-message').textContent = error.message; }
  finally { button.disabled = false; }
});
