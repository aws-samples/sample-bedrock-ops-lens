import React from 'react';
import ReactDOM from 'react-dom/client';
import '@cloudscape-design/global-styles/index.css';
// Project-specific overrides + custom components (.ops-timeline, .action-orange,
// freshness pill, etc.). MUST come AFTER Cloudscape so our selectors win.
import './index.css';
import App from './App.jsx';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
