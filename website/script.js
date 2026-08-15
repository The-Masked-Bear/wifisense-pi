

/* ====== CUSTOM CURSOR ====== */
const cursor = document.getElementById('cursor');
const ring = document.getElementById('cursor-ring');
let mouseX = 0, mouseY = 0;
let ringX = 0, ringY = 0;

document.addEventListener('mousemove', e => {
    mouseX = e.clientX;
    mouseY = e.clientY;
    cursor.style.left = mouseX - 5 + 'px';
    cursor.style.top = mouseY - 5 + 'px';
});

function animateRing() {
    ringX += (mouseX - ringX) * 0.15;
    ringY += (mouseY - ringY) * 0.15;
    ring.style.left = ringX - 20 + 'px';
    ring.style.top = ringY - 20 + 'px';
    requestAnimationFrame(animateRing);
}
animateRing();

const hoverTargets = document.querySelectorAll('a, button, .brutal-card, .faq-question, .brutal-terminal, .giant-text-block, .brutal-sticker');
hoverTargets.forEach(el => {
    el.addEventListener('mouseenter', () => ring.classList.add('hover'));
    el.addEventListener('mouseleave', () => ring.classList.remove('hover'));
});

/* ====== MOUSE TRAIL ====== */
const canvas = document.getElementById('trail-canvas');
const ctx = canvas.getContext('2d');
const trail = [];
const trailColors = ['#FF90E8', '#FFDE59', '#8CFFFB', '#B2FF9E', '#FF5A5F'];

function resizeCanvas() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
}
resizeCanvas();
window.addEventListener('resize', resizeCanvas);

document.addEventListener('mousemove', e => {
    trail.push({
        x: e.clientX,
        y: e.clientY,
        life: 1,
        color: trailColors[Math.floor(Math.random() * trailColors.length)],
        size: Math.random() * 6 + 3
    });
    if (trail.length > 50) trail.shift();
});

function drawTrail() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (let i = trail.length - 1; i >= 0; i--) {
        const p = trail[i];
        p.life -= 0.03;
        if (p.life <= 0) { trail.splice(i, 1); continue; }
        ctx.globalAlpha = p.life;
        ctx.fillStyle = p.color;
        ctx.fillRect(p.x - p.size/2, p.y - p.size/2, p.size, p.size);
    }
    ctx.globalAlpha = 1;
    requestAnimationFrame(drawTrail);
}
drawTrail();

/* ====== SCROLL REVEAL ====== */
const revealObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting) {
            entry.target.classList.add('active');
            revealObserver.unobserve(entry.target);
        }
    });
}, { threshold: 0.15 });

document.querySelectorAll('.reveal').forEach(el => revealObserver.observe(el));

/* ====== STATS COUNTER ====== */
function animateCounter(el) {
    const target = parseInt(el.getAttribute('data-target'));
    const duration = 1500;
    const start = performance.now();
    
    function update(now) {
        const elapsed = now - start;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 4);
        el.textContent = Math.round(eased * target);
        if (progress < 1) requestAnimationFrame(update);
    }
    requestAnimationFrame(update);
}

const statsObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting) {
            entry.target.querySelectorAll('.stat-number').forEach(animateCounter);
            statsObserver.unobserve(entry.target);
        }
    });
}, { threshold: 0.3 });

const statsSection = document.querySelector('.brutal-stats');
if (statsSection) statsObserver.observe(statsSection);

/* ====== FAQ ACCORDION ====== */
document.querySelectorAll('.faq-question').forEach(btn => {
    btn.addEventListener('click', () => {
        const item = btn.parentElement;
        const wasOpen = item.classList.contains('open');
        
        // Close all
        document.querySelectorAll('.faq-item').forEach(i => i.classList.remove('open'));
        
        // Toggle clicked
        if (!wasOpen) item.classList.add('open');
    });
});

/* ====== PARALLAX ====== */
const parallaxLayers = document.querySelectorAll('.parallax-layer');

function handleParallax() {
    const scrollY = window.scrollY;
    parallaxLayers.forEach(layer => {
        const speed = parseFloat(layer.dataset.speed) || 0.5;
        const yPos = -(scrollY * speed * 0.3);
        layer.style.backgroundPosition = `center ${yPos}px`;
    });
}

window.addEventListener('scroll', handleParallax, { passive: true });

/* ====== LIVE CSI DATA FEED ====== */
const csiFeed = document.getElementById('csi-feed');
if (csiFeed) {
    setInterval(() => {
        const line = document.createElement('p');
        line.className = 'csi-line';
        
        // Generate a fake CSI matrix array row
        const numbers = Array.from({length: 4}, () => {
            const real = (Math.random() * 2 - 1).toFixed(3);
            const imag = (Math.random() * 2 - 1).toFixed(3);
            const sign = imag >= 0 ? '+' : '';
            return `${real}${sign}${imag}i`;
        });
        
        line.textContent = `[${numbers.join(', ')}]`;
        csiFeed.prepend(line);
        
        // Prevent memory leak
        if (csiFeed.children.length > 8) {
            csiFeed.removeChild(csiFeed.lastChild);
        }
    }, 120); // Extremely fast updates (120ms) to look like real stream
}

/* ====== CLICK TO COPY ====== */
const copyBtn = document.getElementById('copy-btn');
if (copyBtn) {
    copyBtn.addEventListener('click', () => {
        // Extract the code text from the terminal
        const termLines = document.querySelectorAll('.term-body p:not(.term-success)');
        let codeToCopy = '';
        termLines.forEach(line => {
            // Strip out the > symbol
            codeToCopy += line.textContent.replace(/^>\s*/, '') + '\n';
        });

        navigator.clipboard.writeText(codeToCopy.trim()).then(() => {
            // Feedback
            const originalText = copyBtn.textContent;
            copyBtn.textContent = '[ COPIED! ]';
            copyBtn.classList.remove('color-yellow');
            copyBtn.classList.add('color-mint');
            copyBtn.classList.add('hover-shake');
            
            setTimeout(() => {
                copyBtn.textContent = originalText;
                copyBtn.classList.remove('color-mint');
                copyBtn.classList.add('color-yellow');
                copyBtn.classList.remove('hover-shake');
            }, 2000);
        });
    });
}

/* ====== 3D MAGNETIC CARD TILT ====== */
document.querySelectorAll('.brutal-card').forEach(card => {
    card.addEventListener('mousemove', e => {
        const rect = card.getBoundingClientRect();
        // Mouse position relative to the center of the card
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const centerX = rect.width / 2;
        const centerY = rect.height / 2;
        
        // Calculate rotation limits (-8deg to 8deg)
        const rotateX = ((y - centerY) / centerY) * -8;
        const rotateY = ((x - centerX) / centerX) * 8;
        
        // Apply 3D transform and dynamic shadow
        card.style.transform = `perspective(1000px) rotateX(${rotateX}deg) rotateY(${rotateY}deg) translateZ(10px) scale3d(1.02, 1.02, 1.02)`;
        card.style.boxShadow = `${-rotateY * 1.5 + 10}px ${rotateX * 1.5 + 15}px 0px var(--shadow)`;
        card.style.transition = 'none'; // Snap instantly to mouse
    });
    
    card.addEventListener('mouseleave', () => {
        // Reset to original flat brutalist state
        card.style.transform = 'perspective(1000px) rotateX(0deg) rotateY(0deg) translateZ(0px) scale3d(1, 1, 1)';
        card.style.boxShadow = '10px 10px 0px var(--shadow)';
        card.style.transition = 'transform 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275), box-shadow 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275)';
    });
    
    card.addEventListener('mouseenter', () => {
        // Only override transition when hovered so reveal works on load
        card.style.transition = 'transform 0.1s ease-out, box-shadow 0.1s ease-out';
    });
});

/* ====== INTERACTIVE WAVEFORM CANVAS ====== */
const waveCanvas = document.getElementById('wifi-wave-canvas');
if (waveCanvas) {
    const ctx = waveCanvas.getContext('2d');
    let width = waveCanvas.offsetWidth;
    let height = waveCanvas.offsetHeight;
    waveCanvas.width = width;
    waveCanvas.height = height;
    
    let waveMouse = { x: -1000, y: -1000 };

    const handleWaveMove = (clientX, clientY) => {
        const rect = waveCanvas.getBoundingClientRect();
        waveMouse.x = clientX - rect.left;
        waveMouse.y = clientY - rect.top;
    };
    
    // Mouse Support
    waveCanvas.addEventListener('mousemove', e => handleWaveMove(e.clientX, e.clientY));
    waveCanvas.addEventListener('mouseleave', () => { waveMouse.x = -1000; waveMouse.y = -1000; });
    
    // Touch Support for Mobile
    waveCanvas.addEventListener('touchmove', e => {
        handleWaveMove(e.touches[0].clientX, e.touches[0].clientY);

    }, { passive: false });
    waveCanvas.addEventListener('touchend', () => { waveMouse.x = -1000; waveMouse.y = -1000; });

    let time = 0;
    function drawWaves() {
        ctx.fillStyle = '#111111'; // Always black brutalist background
        ctx.fillRect(0, 0, width, height);

        const waves = [
            { y: height * 0.3, color: '#FF90E8', freq: 0.02, amp: 20, speed: 0.05 },
            { y: height * 0.5, color: '#8CFFFB', freq: 0.015, amp: 30, speed: 0.04 },
            { y: height * 0.7, color: '#B2FF9E', freq: 0.025, amp: 15, speed: 0.06 },
        ];

        waves.forEach(w => {
            ctx.beginPath();
            ctx.moveTo(0, w.y);
            for (let x = 0; x < width; x += 5) {
                let y = w.y + Math.sin(x * w.freq + time * w.speed) * w.amp;

                // Human Interference Logic
                const dx = x - waveMouse.x;
                const dy = y - waveMouse.y;
                const dist = Math.sqrt(dx*dx + dy*dy);

                if (dist < 180) {
                    const interference = (180 - dist) / 180; // 0 to 1 intensity
                    y += (Math.random() - 0.5) * 60 * interference; // Raw noise
                    y += Math.sin(x * 0.4 + time * 2) * 30 * interference; // High freq distortion
                }

                ctx.lineTo(x, y);
            }
            ctx.strokeStyle = w.color;
            ctx.lineWidth = 4;
            ctx.stroke();
        });

        time += 1;
        requestAnimationFrame(drawWaves);
    }
    drawWaves();

    window.addEventListener('resize', () => {
        width = waveCanvas.offsetWidth;
        height = waveCanvas.offsetHeight;
        waveCanvas.width = width;
        waveCanvas.height = height;
    });
}

/* ====== FULLY TYPEABLE TERMINAL ====== */
const termInput = document.getElementById('term-input');
const termOutput = document.getElementById('term-output');
const termContainer = document.getElementById('term-container');

if (termInput && termOutput) {
    // Focus input when clicking anywhere in the terminal body
    termContainer.addEventListener('click', () => termInput.focus());

    termInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            const cmd = termInput.value.trim();
            termInput.value = '';
            if (cmd === '') return;

            // Echo command
            const echo = document.createElement('p');
            echo.innerHTML = `<span style="color: var(--red); font-weight: 900;">></span> ${cmd}`;
            termOutput.appendChild(echo);

            // Process command
            const response = document.createElement('p');
            response.style.opacity = '0.8';
            response.style.marginTop = '5px';
            response.style.marginBottom = '15px';

            switch(cmd.toLowerCase()) {
                case 'help':
                    response.innerHTML = `Available commands:<br>- <span style="color:var(--yellow)">whoami</span><br>- <span style="color:var(--yellow)">ls</span><br>- <span style="color:var(--yellow)">clear</span><br>- <span style="color:var(--yellow)">run wifisense</span>`;
                    break;
                case 'whoami':
                    response.textContent = 'guest@wifisense-pi';
                    break;
                case 'ls':
                    response.innerHTML = `README.md &nbsp;&nbsp; src/ &nbsp;&nbsp; data/ &nbsp;&nbsp; <span style="color:var(--mint)">wifisense.py</span>`;
                    break;
                case 'clear':
                    termOutput.innerHTML = '';
                    return; // Avoid appending empty response
                case 'run wifisense':
                    response.innerHTML = `<span style="color:var(--mint); font-weight:bold;">INITIATING CSI CAPTURE...</span><br>Bypassing optics... [OK]<br>Decrypting stream... [OK]<br>Target Acquired.`;
                    
                    // Easter Egg Hack Animation
                    const terminalBox = document.querySelector('.brutal-terminal');
                    terminalBox.classList.add('terminal-hacked');
                    setTimeout(() => {
                        terminalBox.classList.remove('terminal-hacked');
                    }, 4000);
                    break;
                default:
                    response.textContent = `bash: ${cmd}: command not found`;
            }
            termOutput.appendChild(response);

            // Auto scroll to bottom
            termContainer.scrollTop = termContainer.scrollHeight;
        }
    });
}
