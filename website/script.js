// Intersection Observer for Scroll Animations
document.addEventListener('DOMContentLoaded', () => {
    const observerOptions = {
        root: null,
        rootMargin: '0px',
        threshold: 0.15
    };

    const observer = new IntersectionObserver((entries, observer) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('active');
                observer.unobserve(entry.target); // Run once
            }
        });
    }, observerOptions);

    const revealElements = document.querySelectorAll('.reveal');
    revealElements.forEach(el => observer.observe(el));
});

// Subtle 3D mouse move effect on hero image
const heroImage = document.querySelector('.hero-image-wrapper');
if (heroImage) {
    document.addEventListener('mousemove', (e) => {
        const xAxis = (window.innerWidth / 2 - e.pageX) / 50;
        const yAxis = (window.innerHeight / 2 - e.pageY) / 50;
        heroImage.style.transform = `perspective(1000px) rotateY(${xAxis}deg) rotateX(${yAxis + 5}deg)`;
    });

    // Reset when mouse leaves
    document.addEventListener('mouseleave', () => {
        heroImage.style.transform = `perspective(1000px) rotateY(0deg) rotateX(5deg)`;
    });
}