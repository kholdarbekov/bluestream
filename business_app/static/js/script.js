(function($) {

	"use strict";

	// The theme's own script, cut down to what the storefront still uses (site review SITE-05, 2026-11-14): the
	// header's scrolled state and the scroll-to-top button. The carousels, WOW, fancybox, counters, validation,
	// nice-select and preloader it drove are gone with their plugins: no template carries their markup any more.

	//Header state and Scroll to Top. The header is sticky (ae-ui.css): .fixed-header only adds its shadow and the
	//smaller logo once the page moves; the scroll-to-top button appears after most of a screen. On phones it shows
	//only while the page moves up and steps aside after a short pause, so it never sits over prices, statuses or the
	//footer's accordions for long.
	var phoneQuery = window.matchMedia ? window.matchMedia('(max-width: 767px)') : null;
	var lastScrollTop = 0;
	var toTopHideTimer = null;

	function setToTop(open) {
		$('.scroll-top').toggleClass('open', open);
	}

	function headerStyle() {
		if($('.main-header').length){
			var windowpos = $(window).scrollTop();
			var past = windowpos >= window.innerHeight * 0.8;
			$('.main-header').toggleClass('fixed-header', windowpos >= 8);
			if (phoneQuery && phoneQuery.matches) {
				if (!past || windowpos > lastScrollTop + 2) {
					clearTimeout(toTopHideTimer);
					setToTop(false);
				} else if (windowpos < lastScrollTop - 2) {
					setToTop(true);
					clearTimeout(toTopHideTimer);
					toTopHideTimer = setTimeout(function () {
						if (!$('.scroll-top').is(':focus')) setToTop(false);
					}, 2000);
				}
			} else {
				setToTop(past);
			}
			lastScrollTop = windowpos;
		}
	}

	headerStyle();

	//The navigation menus and the mobile drawer are server-rendered and run from pages/base.js (no cloning here).

	// Scroll to a Specific Div. Back to top also takes keyboard focus to the top of the page (the logo): the button
	// hides itself up there, and focus left on a hidden button would drop out of the page.
	if($('.scroll-to-target').length){
		$(".scroll-to-target").on('click', function() {
			var target = $(this).attr('data-target');
			var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
			$('html, body').animate({
				scrollTop: $(target).offset().top
			}, reduce ? 0 : 600);
			if ($(this).hasClass('scroll-top')) {
				var top = document.querySelector('.ae-header__logo');
				if (top) top.focus({ preventScroll: true });
			}
		});
	}

	$(window).on('scroll', function() {
		headerStyle();
	});

})(window.jQuery);
