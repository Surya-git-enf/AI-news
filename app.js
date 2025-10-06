const loadNewsBtn = document.getElementById('loadNews');
const newsContainer = document.getElementById('newsContainer');
const loader = document.getElementById('loader');

loadNewsBtn.addEventListener('click', () => {
  newsContainer.innerHTML = '';
  loader.style.display = 'block';

  // ✅ Send to your n8n webhook
  const bodyData = {
    method: "POST",
    link: "https://n8n-8ush.onrender.com/webhook/feed",
    body: { email: "suriayrus030@gmail.com" }
  };

  fetch("https://n8n-8ush.onrender.com/webhook-test/request", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(bodyData)
  })
    .then(res => {
      if (!res.ok) throw new Error(`HTTP error! Status: ${res.status}`);
      return res.json().catch(() => ({})); // handle if response is not JSON
    })
    .then(data => {
      console.log("✅ Success:", data);

      loader.style.display = 'none'; // ✅ hide loader once data arrives
      const output = data.output || data; // handle both {output: {...}} or direct {...}

      let i = 1;
      let found = false;

      while (output[`headline${i}`]) {
        const card = document.createElement('div');
        card.className = 'newsCard';
        card.innerHTML = `
          <img src="${output[`image${i}`] || 'default.jpg'}" alt="News Image">
          <h3>${output[`headline${i}`]}</h3>
          <p>${output[`news${i}`]}</p>
          <a href="${output[`link${i}`]}" target="_blank">Read more</a>
          <p style="font-size:0.8rem;color:#ccc;">Categories: ${output[`categories${i}`]}</p>
        `;
        newsContainer.appendChild(card);
        i++;
        found = true;
      }

      if (!found) {
        newsContainer.innerHTML = '<p>No news found.</p>';
      }
    })
    .catch(err => {
      loader.style.display = 'none';
      newsContainer.innerHTML = '<p>Error loading news ❌</p>';
      console.error('❌ Fetch Error:', err);
    });
});
