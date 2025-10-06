const loadNewsBtn = document.getElementById('loadNews');
const newsContainer = document.getElementById('newsContainer');
const loader = document.getElementById('loader');

loadNewsBtn.addEventListener('click', () => {
  // Clear old news
  newsContainer.innerHTML = '';
  loader.style.display = 'block'; // ✅ show loader immediately

  try {
    const bodyData = {
      method: "POST",
      link: "https://n8n-8ush.onrender.com/webhook/feed",
      body: { email: "suriayrus030@gmail.com" }
    };

    const response = await fetch("https://n8n-8ush.onrender.com/webhook-test/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(bodyData)
    });

    if (!response.ok) throw new Error(`HTTP error! Status: ${response.status}`);

    // Try to parse JSON safely
    let data = {};
    try {
      data = await response.json();
    } catch {
      console.warn("⚠️ Non-JSON response received");
    }

    console.log("✅ Success:", data);

    loader.style.display = 'none'; // ✅ hide loader after response
    const output = data.output || data;
    let i = 1;
    let found = false;

    while (output[`headline${i}`]) {
      const card = document.createElement('div');
      card.className = 'newsCard';
      card.innerHTML = `
        <img src="${output[`image${i}`] || 'https://via.placeholder.com/400x200?text=No+Image'}" alt="News Image">
        <h3>${output[`headline${i}`]}</h3>
        <p>${output[`news${i}`]}</p>
        <a href="${output[`link${i}`]}" target="_blank">Read more</a>
        <p style="font-size:0.8rem;color:#ccc;">Categories: ${output[`categories${i}`] || 'General'}</p>
      `;
      newsContainer.appendChild(card);
      i++;
      found = true;
    }

    if (!found) {
      newsContainer.innerHTML = '<p>No news found 📰</p>';
    }

  } catch (err) {
    console.error('❌ Error:', err);
    loader.style.display = 'none';
    newsContainer.innerHTML = '<p>Error loading news ❌</p>';
  }
});
