/**
 * Custom JavaScript for Index Page - Using existing styles only
 * Water Delivery Business Platform
 */

$(document).ready(function() {
    
    // Initialize banner carousel
    if ($('.banner-carousel').length) {
        $('.banner-carousel').owlCarousel({
            loop: true,
            margin: 0,
            nav: false,
            dots: true,
            autoplay: true,
            autoplayTimeout: 5000,
            autoplayHoverPause: true,
            responsive: {
                0: { items: 1 },
                600: { items: 1 },
                1000: { items: 1 }
            }
        });
    }
    
    // Initialize testimonials carousel
    if ($('.three-item-carousel').length) {
        $('.three-item-carousel').owlCarousel({
            loop: true,
            margin: 30,
            nav: false,
            dots: true,
            autoplay: true,
            autoplayTimeout: 6000,
            responsive: {
                0: { items: 1 },
                768: { items: 2 },
                1200: { items: 3 }
            }
        });
    }
    
    // Initialize single-item testimonial carousel
    if ($('.single-item-carousel').length) {
        $('.single-item-carousel').owlCarousel({
            loop: true,
            margin: 0,
            nav: false,
            dots: true,
            autoplay: true,
            autoplayTimeout: 7000,
            items: 1
        });
    }
    
    // Cart functionality
    window.addToCart = function(productId) {
        let cartItems = JSON.parse(localStorage.getItem('cart') || '[]');
        
        const existingItem = cartItems.find(item => item.product_id === productId);
        
        if (existingItem) {
            existingItem.quantity += 1;
        } else {
            cartItems.push({
                product_id: productId,
                quantity: 1,
                added_at: new Date().toISOString()
            });
        }
        
        localStorage.setItem('cart', JSON.stringify(cartItems));
        updateCartCounter();
        
        // Simple alert instead of custom notification
        alert('Product added to cart successfully!');
    };
    
    // Update cart counter in header
    function updateCartCounter() {
        const cartItems = JSON.parse(localStorage.getItem('cart') || '[]');
        const totalItems = cartItems.reduce((sum, item) => sum + item.quantity, 0);
        
        const cartCounters = document.querySelectorAll('#cart-count, #cart-count-sticky, .cart-box span');
        cartCounters.forEach(counter => {
            counter.textContent = totalItems;
            counter.style.display = totalItems > 0 ? 'inline' : 'none';
        });
    }
    
    // Initialize cart counter on page load
    updateCartCounter();
    
    // Pricing tabs functionality
    $('.tab-btn').on('click', function() {
        const tabId = $(this).attr('data-tab');
        
        // Remove active class from all tabs and buttons
        $('.tab-btn').removeClass('active-btn');
        $('.pr-tab').removeClass('active-tab');
        
        // Add active class to clicked button and corresponding tab
        $(this).addClass('active-btn');
        $(tabId).addClass('active-tab');
    });
});