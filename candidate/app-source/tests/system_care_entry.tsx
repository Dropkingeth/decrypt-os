// Test-only mount. The packaged UI imports SystemCare from its normal screen.
import React from 'react';
import {createRoot} from 'react-dom/client';
import {SystemCare} from '../app/ui/src/SystemCare';
import '../app/ui/src/styles.css';
createRoot(document.getElementById('root')!).render(<SystemCare/>);
