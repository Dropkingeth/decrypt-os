// Actual runtime component, local API fixtures only.
import React from 'react';
import {createRoot} from 'react-dom/client';
import {TradingRuntime} from '../app/ui/src/TradingRuntime';
import '../app/ui/src/styles.css';
createRoot(document.getElementById('root')!).render(<TradingRuntime onConnections={()=>{}}/>);
