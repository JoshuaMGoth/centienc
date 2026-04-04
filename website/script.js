function initClock() {
    const heroClock = document.getElementById("heroClock");
    const formatTime = () => new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });

    if (heroClock) {
        heroClock.textContent = formatTime();
        setInterval(() => {
            heroClock.textContent = formatTime();
        }, 1000);
    }
}

function initMobileNav() {
    const toggle = document.querySelector(".mobile-toggle");
    const nav = document.querySelector(".nav");

    if (!toggle || !nav) {
        return;
    }

    toggle.addEventListener("click", () => {
        const isOpen = nav.classList.toggle("nav-open");
        toggle.setAttribute("aria-expanded", String(isOpen));
    });

    nav.querySelectorAll("a").forEach((link) => {
        link.addEventListener("click", () => {
            nav.classList.remove("nav-open");
            toggle.setAttribute("aria-expanded", "false");
        });
    });
}

function initReveal() {
    const elements = document.querySelectorAll(".reveal");

    if (!elements.length) {
        return;
    }

    const observer = new IntersectionObserver(
        (entries) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    entry.target.classList.add("reveal-visible");
                    observer.unobserve(entry.target);
                }
            });
        },
        { threshold: 0.12 }
    );

    elements.forEach((element) => observer.observe(element));
}

function initCounters() {
    const counters = document.querySelectorAll(".counter");

    if (!counters.length) {
        return;
    }

    const animateCounter = (element) => {
        const target = Number(element.dataset.target || 0);
        const duration = 1200;
        const start = performance.now();
        const isDecimal = !Number.isInteger(target);

        const tick = (now) => {
            const progress = Math.min((now - start) / duration, 1);
            const eased = 1 - Math.pow(1 - progress, 3);
            const value = target * eased;
            element.textContent = isDecimal ? value.toFixed(1) : Math.round(value).toString();

            if (progress < 1) {
                requestAnimationFrame(tick);
            }
        };

        requestAnimationFrame(tick);
    };

    const observer = new IntersectionObserver(
        (entries) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    animateCounter(entry.target);
                    observer.unobserve(entry.target);
                }
            });
        },
        { threshold: 0.5 }
    );

    counters.forEach((counter) => observer.observe(counter));
}

document.addEventListener("DOMContentLoaded", () => {
    if (window.lucide) {
        window.lucide.createIcons();
    }

    initClock();
    initMobileNav();
    initReveal();
    initCounters();
});
