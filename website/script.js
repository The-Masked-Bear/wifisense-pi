

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
        
        // Chromium Compositor Bug Fix: Maintain a pure text array
        if (!csiFeed.lines) csiFeed.lines = [];
        
        const numbers = Array.from({length: 4}, () => {
            const real = (Math.random() * 2 - 1).toFixed(3);
            const imag = (Math.random() * 2 - 1).toFixed(3);
            const sign = imag >= 0 ? '+' : '';
            return `${real}${sign}${imag}i`;
        });
        
        csiFeed.lines.unshift(`[${numbers.join(', ')}]`);
        if (csiFeed.lines.length > 7) csiFeed.lines.pop();
        
        // Use raw innerHTML string joining to force paint
        csiFeed.innerHTML = csiFeed.lines.map((text, i) => 
            `<p class="csi-line" style="${i===0 ? 'opacity:1; color:var(--white); font-weight:bold;' : ''}">${text}</p>`
        ).join('');
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

/* ====== FULLY TYPEABLE TERMINAL & LINUX SIMULATOR ====== */
const termInput = document.getElementById('term-input');
const termOutput = document.getElementById('term-output');
const termContainer = document.getElementById('term-container');

// Virtual File System
const vfs = {
    'README.md': 'WiFi Sense V1.0.0\nPassive 802.11 CSI detection system.\nAuthor: The-Masked-Bear',
    'secrets.txt': 'ERROR: ACCESS DENIED. KEY: HUNTER2',
    'config.json': '{\n  "tx_mac": "00:11:22:33:44:55",\n  "rx_mac": "AA:BB:CC:DD:EE:FF",\n  "freq": 2.4\n}',
    'wifisense.py': 'import numpy as np\nprint("Extracting subcarriers...")',
    'passwords.db': 'admin: admin123\nguest: guest\nbear: honeypot'
};
let cwd = '~/wifisense';
let cmdHistory = [];
let historyIndex = -1;

if (termInput && termOutput) {
    termContainer.addEventListener('click', () => termInput.focus());

    termInput.addEventListener('keydown', e => {
        if (e.key === 'ArrowUp') {
            if (historyIndex < cmdHistory.length - 1) {
                historyIndex++;
                termInput.value = cmdHistory[cmdHistory.length - 1 - historyIndex];
            }
            e.preventDefault();
        } else if (e.key === 'ArrowDown') {
            if (historyIndex > 0) {
                historyIndex--;
                termInput.value = cmdHistory[cmdHistory.length - 1 - historyIndex];
            } else {
                historyIndex = -1;
                termInput.value = '';
            }
            e.preventDefault();
        } else if (e.key === 'Enter') {
            const cmdStr = termInput.value.trim();
            termInput.value = '';
            historyIndex = -1;
            
            if (cmdStr !== '') cmdHistory.push(cmdStr);

            // Echo command
            const echo = document.createElement('p');
            echo.innerHTML = `<span style="color: var(--red); font-weight: 900;">guest@wifisense:${cwd}$</span> ${cmdStr}`;
            termOutput.appendChild(echo);

            if (cmdStr === '') return;

            const args = cmdStr.split(' ').filter(Boolean);
            const cmd = args[0].toLowerCase();
            
            const response = document.createElement('p');
            response.style.opacity = '0.8';
            response.style.marginTop = '5px';
            response.style.marginBottom = '15px';
            response.style.whiteSpace = 'pre-wrap';

            let out = '';
            switch(cmd) {
                case 'help':
                    out = `GNU bash, version 5.1.4(1)-release (aarch64-unknown-linux-gnu)\nAvailable commands:\nls, cd, pwd, cat, touch, rm, echo, whoami, uname, date, uptime, ping, neofetch, history, clear, sudo, run, gravity, matrix`;
                    break;
                case 'ls':
                    out = Object.keys(vfs).map(f => f.endsWith('.py') ? `<span style="color:var(--mint)">${f}</span>` : f).join(' &nbsp;&nbsp; ');
                    break;
                case 'pwd':
                    out = `/home/guest/${cwd.replace('~/', '')}`;
                    break;
                case 'cd':
                    if (!args[1] || args[1] === '~') cwd = '~';
                    else if (args[1] === '..') cwd = cwd.split('/').slice(0, -1).join('/') || '/';
                    else cwd = args[1].startsWith('/') ? args[1] : `${cwd}/${args[1]}`;
                    break;
                case 'cat':
                    if (!args[1]) out = 'cat: missing operand';
                    else if (vfs[args[1]]) out = vfs[args[1]];
                    else out = `cat: ${args[1]}: No such file or directory`;
                    break;
                case 'touch':
                    if (args[1]) { vfs[args[1]] = ''; out = ''; }
                    else out = 'touch: missing file operand';
                    break;
                case 'rm':
                    if (!args[1]) out = 'rm: missing operand';
                    else if (args[1] === '-rf' && args[2] === '/') {
                        out = `<span style="color:var(--red); font-weight:bold; font-size:1.2rem;">CRITICAL WARNING: DELETING FILESYSTEM...</span>`;
                        setTimeout(() => {
                            document.querySelectorAll('section, header, nav, footer, .brutal-marquee').forEach((el, i) => {
                                setTimeout(() => {
                                    el.style.transition = 'transform 0.2s, opacity 0.2s';
                                    el.style.transform = 'scale(0.5)';
                                    el.style.opacity = '0';
                                    setTimeout(() => el.style.display = 'none', 200);
                                }, i * 400);
                            });
                        }, 1000);
                    }
                    else if (vfs[args[1]]) { delete vfs[args[1]]; out = ''; }
                    else out = `rm: cannot remove '${args[1]}': No such file or directory`;
                    break;
                case 'echo':
                    out = args.slice(1).join(' ');
                    break;
                case 'whoami':
                    out = 'guest';
                    break;
                case 'uname':
                    out = args[1] === '-a' ? 'Linux wifisense-pi 6.1.21-v8+ #1 SMP PREEMPT aarch64 GNU/Linux' : 'Linux';
                    break;
                case 'date':
                    out = new Date().toString();
                    break;
                case 'uptime':
                    out = 'up 1337 days,  4:20,  1 user,  load average: 0.01, 0.05, 0.00';
                    break;
                case 'history':
                    out = cmdHistory.map((h, i) => `  ${i+1}  ${h}`).join('\n');
                    break;
                case 'ping':
                    const target = args[1] || '1.1.1.1';
                    out = `PING ${target} (${target}): 56 data bytes\n64 bytes from ${target}: icmp_seq=0 ttl=64 time=12.4 ms\n64 bytes from ${target}: icmp_seq=1 ttl=64 time=14.2 ms\n64 bytes from ${target}: icmp_seq=2 ttl=64 time=11.9 ms`;
                    break;
                case 'sudo':
                    out = `guest is not in the sudoers file. This incident will be reported to The-Masked-Bear.`;
                    break;
                case 'neofetch':
                    out = `<pre style="color:var(--mint); font-size:10px; line-height:10px; margin:0;">
       _,met$$$$$gg.          <span style="color:var(--white)">guest@wifisense</span>
    ,g$$$$$$$$$$$$$$$P.       <span style="color:var(--white)">---------------</span>
  ,g$$P"     """Y$$.".        <span style="color:var(--white)">OS:</span> Debian GNU/Linux 11 aarch64
 ,$$P'              `$$$.     <span style="color:var(--white)">Host:</span> Raspberry Pi 4 Model B
',$$P       ,ggs.     `$$b:   <span style="color:var(--white)">Kernel:</span> 6.1.21-v8+
`d$$'     ,$P"'   .    $$$    <span style="color:var(--white)">Uptime:</span> 1337 days
 $$P      d$'     ,    $$P    <span style="color:var(--white)">Packages:</span> 420 (dpkg)
 $$:      $$.   -    ,d$$'    <span style="color:var(--white)">Shell:</span> bash 5.1.4
 $$;      Y$b._   _,d$P'      <span style="color:var(--white)">Terminal:</span> brutal-term
 Y$$.    `.`"Y$$$$P"'         <span style="color:var(--white)">CPU:</span> BCM2835 (4) @ 1.5GHz
 `$$b      "-.__              <span style="color:var(--white)">Memory:</span> 842MiB / 3804MiB
  `Y$$
</pre>`;
                    break;
                case 'run':
                    if (args[1] === 'wifisense') {
                        out = `<span style="color:var(--mint); font-weight:bold;">INITIATING CSI CAPTURE...</span>\nBypassing optics... [OK]\nDecrypting stream... [OK]\nTarget Acquired.`;
                        const terminalBox = document.querySelector('.brutal-terminal');
                        terminalBox.classList.add('terminal-hacked');
                        setTimeout(() => terminalBox.classList.remove('terminal-hacked'), 4000);
                    } else {
                        out = `run: missing target`;
                    }
                    break;
                case 'clear':
                    termOutput.innerHTML = '';
                    return;
                
                // === SILLY EASTER EGGS ===
                case 'bear':
                    out = `<pre style="font-size:10px; line-height:10px;">
  ʕ·͡ᴥ·ʔ
 /|__|\\
  "  "  
RAWR.
</pre>`;
                    break;
                case 'do':
                    if (args.slice(1).join(' ') === 'a barrel roll') {
                        document.body.style.transition = "transform 2s ease-in-out";
                        document.body.style.transform = "rotate(360deg)";
                        setTimeout(() => document.body.style.transform = "none", 2000);
                        out = "Rolling...";
                    }
                    break;
                case 'gravity':
                    out = "Gravity enabled. Good luck.";
                    document.querySelectorAll('.brutal-card, .term-header, .stat-item, h1, .brutal-btn').forEach(el => {
                        el.style.transition = "transform 2.5s cubic-bezier(0.5, 0, 1, 1)";
                        el.style.transform = `translateY(${window.innerHeight}px) rotate(${Math.random()*90-45}deg)`;
                    });
                    break;
                case 'matrix':
                    document.body.classList.toggle('matrix-mode');
                    out = "Follow the white rabbit.";
                    break;
                default:
                    out = `bash: ${cmd}: command not found`;
            }
            
            if (out !== '') {
                response.innerHTML = out;
                termOutput.appendChild(response);
            }
            
            termContainer.scrollTop = termContainer.scrollHeight;
        }
    });
}

/* ====== ADDITIONAL EASTER EGGS ====== */

// 1. Clickable Terminal Window Controls
const yellowDot = document.querySelector('.term-header .bg-yellow');
if (yellowDot) {
    yellowDot.style.cursor = 'pointer';
    yellowDot.addEventListener('click', () => {
        const term = document.querySelector('.brutal-terminal');
        const isMin = term.style.maxHeight === '50px';
        term.style.transition = 'max-height 0.4s ease';
        term.style.overflow = 'hidden';
        term.style.maxHeight = isMin ? '1000px' : '50px';
    });
}

const greenDot = document.querySelector('.term-header .bg-green');
if (greenDot) {
    greenDot.style.cursor = 'pointer';
    greenDot.addEventListener('click', () => {
        const term = document.querySelector('.brutal-terminal');
        const isMax = term.style.width === '100vw';
        term.style.transition = 'all 0.4s ease';
        if (isMax) {
            term.style.position = 'relative';
            term.style.width = 'auto';
            term.style.height = 'auto';
            term.style.zIndex = '1';
            term.style.maxWidth = '800px';
            term.style.transform = 'none';
        } else {
            term.style.position = 'fixed';
            term.style.top = '0';
            term.style.left = '0';
            term.style.width = '100vw';
            term.style.height = '100vh';
            term.style.maxWidth = '100vw';
            term.style.zIndex = '999999';
            term.style.transform = 'none';
        }
    });
}

// 2. Clickable Stats Counter (Increments infinitely)
document.querySelectorAll('.stat-number').forEach(stat => {
    stat.style.cursor = 'pointer';
    stat.style.userSelect = 'none';
    stat.addEventListener('click', () => {
        stat.textContent = parseInt(stat.textContent) + 1;
        stat.style.transform = 'scale(1.3)';
        stat.style.color = 'var(--red)';
        setTimeout(() => {
            stat.style.transform = 'scale(1)';
            stat.style.color = 'var(--black)';
        }, 150);
    });
});
